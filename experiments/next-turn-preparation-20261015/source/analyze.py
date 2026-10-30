"""Conservative cache attribution for the fresh rolling-preparation pilot."""

import argparse
import hashlib
import json
from pathlib import Path
import struct
import statistics
import sys

from validate_capture import validate_capture

BLOCK = 16
CACHE_SALT_PREFIX = "dynamo-cache-salt:"


def engine_cache_salt(client_salt):
    return CACHE_SALT_PREFIX + client_salt


def prefix_length(a, b):
    return next(
        (i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b))
    )


def aligned(count):
    return count // BLOCK * BLOCK


def block_keys(tokens, salt):
    digest = hashlib.sha256()
    keys = []
    for start in range(0, aligned(len(tokens)), BLOCK):
        digest.update(struct.pack(">" + "Q" * BLOCK, *tokens[start : start + BLOCK]))
        keys.append((salt, digest.hexdigest()))
    return keys


def index_backend(events):
    entries, links, results, first_output = {}, {}, {}, {}
    for row in events:
        request_id = row.get("request_id")
        if row["kind"] == "engine_enter":
            if request_id in entries:
                raise ValueError("Repeated external request ID: " + str(request_id))
            entries[request_id] = row
        elif row["kind"] == "engine_link":
            links.setdefault(row["external_request_id"], []).append(row)
        elif row["kind"] == "engine_result":
            if request_id in results:
                raise ValueError("Repeated terminal record: " + str(request_id))
            results[request_id] = row
        elif row["kind"] == "engine_output":
            first_output.setdefault(request_id, row)
    return entries, links, results, first_output


def generated(result):
    outputs = result.get("outputs") or {}
    if set(outputs) - {"0", 0}:
        raise ValueError("The pilot requires exactly one output choice")
    if result.get("output_ids_status") != "reported":
        return None
    return outputs.get("0", outputs.get(0, []))


def foreground_ceiling(request_id, normal_ids, entries, results, first_output):
    """Overestimate ordinary-history availability; only excess gets prep credit."""
    current = entries[request_id]
    output = first_output.get(request_id)
    if output is None:
        return None
    bound = output["worker_monotonic_ns"]
    best = 0
    for other in normal_ids:
        if other == request_id or other not in entries:
            continue
        entry = entries[other]
        if (
            entry["cache_salt"] != current["cache_salt"]
            or entry["worker_monotonic_ns"] >= bound
        ):
            continue
        terminal = results.get(other)
        if terminal is None or generated(terminal) is None:
            return None
        candidate = entry["prompt_ids"] + generated(terminal)
        common = prefix_length(current["prompt_ids"], candidate)
        # Cancellation can hide later generated tokens; a known divergence still bounds reuse.
        if terminal.get("terminal") != "finished" and common == len(candidate) < len(current["prompt_ids"]):
            return None
        # Including eventual output and the final sampled token overbounds foreground work.
        best = max(best, aligned(common))
    return best


def delta_ms(end, start):
    return (
        (end - start) / 1e6
        if isinstance(end, (int, float))
        and isinstance(start, (int, float))
        and end >= start
        else None
    )


def latency_fields(client, result):
    start = client.get("started_monotonic_ns")
    stats = (result or {}).get("engine_stats") or {}

    def stage(end, begin):
        a, b = stats.get(end), stats.get(begin)
        return (
            (a - b) * 1000
            if isinstance(a, (int, float))
            and isinstance(b, (int, float))
            and a > 0
            and b > 0
            and a >= b
            else None
        )

    return {
        "client_total_ms": delta_ms(client.get("ended_monotonic_ns"), start),
        "client_first_generated_ms": delta_ms(
            client.get("first_generated_monotonic_ns"), start
        ),
        "client_first_semantic_ms": delta_ms(
            client.get("first_semantic_monotonic_ns"), start
        ),
        "native_first_token_latency_s": stats.get("first_token_latency"),
        "native_queue_ms": stage("scheduled_ts", "queued_ts"),
        "native_first_token_after_schedule_ms": stage("first_token_ts", "scheduled_ts"),
        "native_decode_span_ms": stage("last_token_ts", "first_token_ts"),
        "native_stats": stats,
    }


def analyze_trial(directory, index, render_ids=None):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest["mode"] != "live":
        raise ValueError("Scripted output cannot be interpreted as model results")
    reset = json.loads((directory / "reset.json").read_text())
    if not all(
        reset.get(k) is True
        for k in ("backend_reset_reported", "kv_clear_event_verified", "idle_verified")
    ):
        raise ValueError("Missing clean-start evidence")
    entries, links, results, first_output = index
    clients = [
        row
        for path in directory.glob("*-requests.json")
        for row in json.loads(path.read_text())
    ]
    preps = [
        json.loads(path.read_text()) for path in directory.glob("preparations/*/*.json")
    ]
    attempts = [
        row
        for path in directory.glob("*-preparations.json")
        for row in json.loads(path.read_text())
    ]
    normal_ids = {row["client_request_id"] for row in clients}
    prep_ids = {row["client_request_id"] for row in preps}
    if (
        len(normal_ids) != len(clients)
        or len(prep_ids) != len(preps)
        or normal_ids & prep_ids
    ):
        raise ValueError("Client request IDs are not unique")
    client_salt = manifest["trial_id"]
    salt = engine_cache_salt(client_salt)
    errors = []
    events_path = directory / "events.jsonl"
    if events_path.exists():
        client_events = [
            json.loads(line) for line in events_path.read_text().splitlines()
        ]
        sent = {
            r["client_request_id"]
            for r in client_events
            if r["kind"] == "http_request_link"
        }
        if sent != normal_ids | prep_ids:
            errors.append("Client HTTP linkage/capture set is incomplete")
    else:
        errors.append("Client event capture is absent")
    attempt_ids = {r["id"] for r in attempts}
    if any(r["label"] not in attempt_ids for r in preps):
        errors.append("Preparation transport lacks a trigger attempt")
    trial_backend = {
        rid for rid, row in entries.items() if row.get("cache_salt") == salt
    }
    if trial_backend - normal_ids - prep_ids:
        errors.append("Unaccounted backend traffic in trial cache identity")
    for client in [*clients, *preps]:
        rid = client["client_request_id"]
        entry = entries.get(rid)
        if entry is None:
            if client.get("done_event") is True or client.get("status") == "completed":
                errors.append(f"{rid}: completed HTTP request missing at backend")
            continue
        if entry.get("cache_salt") != salt:
            errors.append(f"{rid}: cache salt mismatch")
        mapping = links.get(rid, [])
        if (
            len(mapping) != 1
            or mapping[0]["processed_prompt_ids"] != entry["prompt_ids"]
            or mapping[0]["capture_id"] != entry["capture_id"]
        ):
            errors.append(f"{rid}: missing or inconsistent engine linkage")
        expected = client["request"].get("prompt")
        if expected is None and render_ids is not None:
            expected = render_ids(client["request"])
        if expected is None or expected != entry["prompt_ids"]:
            errors.append(f"{rid}: input token parity unverified or mismatched")
    rows = []
    completed_preps = {
        rid
        for rid in prep_ids
        if rid in results
        and results[rid]["terminal"] == "finished"
        and generated(results[rid])
    }
    ineligible_reasons = {
        row["trigger_request_id"]: row.get("reason")
        for row in attempts
        if row.get("status") == "ineligible" and row.get("trigger_request_id")
    }
    for client in clients:
        rid = client["client_request_id"]
        entry, result = entries.get(rid), results.get(rid)
        row = {
            "request_id": rid,
            "session_id": client["session_id"],
            "user_turn": sum(
                x.get("role") == "user" for x in client["request"]["messages"]
            )
            - 1,
            "client_error": client.get("error"),
            "finish_reason": client.get("finish_reason"),
            "response_issue": client.get("response_issue") or ineligible_reasons.get(rid),
            "backend_observed": entry is not None,
            **latency_fields(client, result),
            "worker_entry_to_first_output_ms": delta_ms(
                first_output.get(rid, {}).get("worker_monotonic_ns"),
                entry.get("worker_monotonic_ns") if entry else None,
            ),
        }
        ceiling = (
            foreground_ceiling(rid, normal_ids, entries, results, first_output)
            if entry
            else None
        )
        cached = (result or {}).get("cached_tokens")
        credit = None
        matching_completed_prep = 0
        if entry:
            for prep_id in completed_preps:
                prep = entries[prep_id]
                if (
                    prep["cache_salt"] == salt
                    and results[prep_id]["worker_monotonic_ns"]
                    < entry["worker_monotonic_ns"]
                ):
                    matching_completed_prep = max(
                        matching_completed_prep,
                        aligned(prefix_length(entry["prompt_ids"], prep["prompt_ids"])),
                    )
        if (
            not errors
            and ceiling is not None
            and type(cached) is int
            and 0 <= cached <= len(entry["prompt_ids"])
        ):
            credit = max(0, min(aligned(cached), matching_completed_prep) - ceiling)
        row.update(
            prompt_tokens=len(entry["prompt_ids"]) if entry else None,
            cached_tokens=cached,
            foreground_history_ceiling_tokens=ceiling,
            matching_completed_preparation_tokens=matching_completed_prep,
            preparation_credit_lower_bound_tokens=credit,
            cached_tokens_above_foreground_ceiling=max(0, aligned(cached) - ceiling)
            if type(cached) is int and ceiling is not None
            else None,
        )
        if (
            manifest["condition"] == "off"
            and row["cached_tokens_above_foreground_ceiling"]
        ):
            errors.append(f"{rid}: hint-off cache exceeds foreground-history ceiling")
        rows.append(row)
    prep_rows, prepared_blocks, matched_later_blocks = [], set(), set()
    for client in preps:
        rid = client["client_request_id"]
        entry, result = entries.get(rid), results.get(rid)
        done = rid in completed_preps
        cached = (result or {}).get("cached_tokens")
        row = {
            "request_id": rid,
            "label": client["label"],
            "client_status": client.get("status"),
            "backend_observed": entry is not None,
            "backend_terminal": (result or {}).get("terminal"),
            "prompt_tokens": len(entry["prompt_ids"]) if entry else None,
            "cached_tokens": cached,
            "computed_partial_prefill_tokens": None,
            "completed_noncached_prompt_tokens": len(entry["prompt_ids"]) - cached
            if done and type(cached) is int
            else None,
            "discarded_sampled_tokens": len(generated(result))
            if result and generated(result) is not None
            else None,
        }
        prep_rows.append(row)
        if done:
            keys = block_keys(entry["prompt_ids"], salt)
            prepared_blocks.update(keys)
            for normal_id in normal_ids:
                normal = entries.get(normal_id)
                if (
                    normal
                    and normal["worker_monotonic_ns"] > result["worker_monotonic_ns"]
                ):
                    count = (
                        aligned(
                            prefix_length(entry["prompt_ids"], normal["prompt_ids"])
                        )
                        // BLOCK
                    )
                    matched_later_blocks.update(keys[:count])
    sessions = [
        json.loads(path.read_text()) for path in directory.glob("sessions/*.json")
    ]
    if any(
        x.get("status") != "written"
        for session in sessions
        for x in session.get("capture_flush", {}).values()
    ):
        errors.append("A preparation capture did not flush successfully")
    if errors:
        for row in rows:
            row["preparation_credit_lower_bound_tokens"] = None
    return {
        "trial_id": client_salt,
        "engine_cache_salt": salt,
        "condition": manifest["condition"],
        "background": manifest["background"],
        "attribution_valid": not errors,
        "errors": errors,
        "normal_requests": rows,
        "preparation_requests": prep_rows,
        "all_attempts": attempts,
        "sessions": [
            {
                k: s.get(k)
                for k in (
                    "session_id",
                    "task_id",
                    "outcome",
                    "turns",
                    "model_calls",
                    "tool_calls",
                    "foreground_elapsed_s",
                    "elapsed_s",
                    "terminal_preparation_drain",
                )
            }
            for s in sessions
        ],
        "preparation_credit_lower_bound_tokens": sum(
            r["preparation_credit_lower_bound_tokens"] or 0 for r in rows
        )
        if not errors
        else None,
        "requests_with_unknown_credit": sum(
            r["preparation_credit_lower_bound_tokens"] is None for r in rows
        ),
        "completed_preparation_prefix_blocks": len(prepared_blocks),
        "completed_preparation_blocks_not_matched_by_later_real_input": len(
            prepared_blocks - matched_later_blocks
        ),
        "canceled_partial_work_is_unknown": True,
    }


def distribution(values):
    values = sorted(x for x in values if x is not None)
    if not values:
        return {"count": 0, "median": None, "p90": None, "total": None}
    position = 0.9 * (len(values) - 1)
    low = int(position)
    p90 = values[low] + (values[min(low + 1, len(values) - 1)] - values[low]) * (
        position - low
    )
    return {
        "count": len(values),
        "median": statistics.median(values),
        "p90": p90,
        "total": sum(values),
    }


def summarize_trial(report):
    summaries = []
    for session in report["sessions"]:
        rows = [
            r
            for r in report["normal_requests"]
            if r["session_id"] == session["session_id"]
        ]
        measured = [
            r
            for r in rows
            if r["cached_tokens"] is not None and r["prompt_tokens"] is not None
        ]
        prompts = sum(r["prompt_tokens"] for r in measured)
        summaries.append(
            {
                **session,
                "request_count": len(rows),
                "requests_missing_cache_usage": len(rows) - len(measured),
                "measured_prompt_tokens": prompts,
                "measured_cached_tokens": sum(r["cached_tokens"] for r in measured),
                "measured_actual_reuse": sum(r["cached_tokens"] for r in measured)
                / prompts
                if prompts
                else None,
                "client_total_ms": distribution([r["client_total_ms"] for r in rows]),
                "client_first_generated_ms": distribution(
                    [r["client_first_generated_ms"] for r in rows]
                ),
                "client_first_semantic_ms": distribution(
                    [r["client_first_semantic_ms"] for r in rows]
                ),
                "native_first_token_latency_s": distribution(
                    [r["native_first_token_latency_s"] for r in rows]
                ),
            }
        )
    deadlines = []
    for row in report["all_attempts"]:
        arrival, ended = (
            row.get("next_dispatch_monotonic_ns"),
            row.get("ended_monotonic_ns"),
        )
        deadlines.append(
            {
                "id": row["id"],
                "status": row["status"],
                "final_attempt_without_resume": arrival is None,
                "completed_before_next_dispatch": ended <= arrival
                if row["status"] == "completed" and arrival is not None
                else None,
                "available_client_window_ms": delta_ms(
                    arrival, row["snapshot_ready_monotonic_ns"]
                )
                if "snapshot_ready_monotonic_ns" in row
                else None,
                "cancel_requested": row.get("cancel_requested_monotonic_ns")
                is not None,
            }
        )
    return {"sessions": summaries, "attempt_deadlines": deadlines}


def paired_differences(trials):
    by_label = {trial["trial_label"]: trial for trial in trials}
    pairs = []
    for load in (0, 1):
        for pair in (0, 1):
            prefix = f"load{load}-pair{pair}"
            off, rolling = (
                by_label.get(prefix + "-off"),
                by_label.get(prefix + "-rolling"),
            )
            row = {
                "pair": prefix,
                "background": bool(load),
                "status": "complete" if off and rolling else "incomplete",
                "sessions": [],
            }
            if off and rolling:
                off_sessions = {s["task_id"]: s for s in off["summary"]["sessions"]}
                on_sessions = {s["task_id"]: s for s in rolling["summary"]["sessions"]}
                for task in sorted(set(off_sessions) | set(on_sessions)):
                    before, after = off_sessions.get(task), on_sessions.get(task)
                    item = {
                        "task_id": task,
                        "status": "complete" if before and after else "incomplete",
                    }
                    if before and after:
                        item.update(
                            off_outcome=before["outcome"],
                            rolling_outcome=after["outcome"],
                        )
                        for metric in (
                            "foreground_elapsed_s",
                            "model_calls",
                            "tool_calls",
                            "measured_prompt_tokens",
                            "measured_cached_tokens",
                        ):
                            a, b = before.get(metric), after.get(metric)
                            item[metric + "_rolling_minus_off"] = (
                                b - a if a is not None and b is not None else None
                            )
                        for metric in (
                            "client_total_ms",
                            "client_first_generated_ms",
                            "client_first_semantic_ms",
                            "native_first_token_latency_s",
                        ):
                            for statistic in ("median", "p90"):
                                a, b = (
                                    before[metric][statistic],
                                    after[metric][statistic],
                                )
                                item[
                                    metric + "_" + statistic + "_rolling_minus_off"
                                ] = b - a if a is not None and b is not None else None
                    row["sessions"].append(item)
            pairs.append(row)
    return pairs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--tokenizer-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    validation = validate_capture(args.engine)
    events = [json.loads(line) for line in args.engine.read_text().splitlines()]
    index = index_backend(events)
    from support import TokenBudget

    budget = TokenBudget(args.tokenizer_dir)
    manifests = sorted(args.runs.glob("*/manifest.json"))
    if (args.runs / "manifest.json").exists():
        manifests = [args.runs / "manifest.json"]
    trials = []
    for path in manifests:
        trial = analyze_trial(path.parent, index, budget.ids)
        trial["trial_label"] = path.parent.name
        trial["summary"] = summarize_trial(trial)
        trials.append(trial)
    report = {
        "worker_capture": validation,
        "block_size": BLOCK,
        "trials": trials,
        "paired_differences": paired_differences(trials),
        "ceiling": "All potentially preceding foreground inputs plus eventual outputs across both sessions; excludes the target itself. This overestimates foreground availability.",
        "timing": "Client and worker clock domains remain separate. Native zero/default timestamps produce unknown intervals.",
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "trials": len(trials),
                "attribution_valid": all(x["attribution_valid"] for x in trials),
                "output": str(args.output),
            }
        )
    )


if __name__ == "__main__":
    main()
