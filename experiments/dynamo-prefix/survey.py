"""Measure prefix continuity in every captured coding run from the GPU pilot."""

import argparse
import hashlib
from importlib.metadata import version
import json
import math
from pathlib import Path
from statistics import mean, median
import tarfile

import audit


ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT.parent / "dynamo-20260928/raw-records.tar.gz"


def describe(values):
    values = sorted(values)
    if not values:
        return {"count": 0, "sum": 0, "mean": None, "median": None, "min": None, "p90": None, "max": None}
    position = (len(values) - 1) * 0.9
    lower = math.floor(position)
    p90 = values[lower] + (values[math.ceil(position)] - values[lower]) * (position - lower)
    return {"count": len(values), "sum": sum(values), "mean": mean(values),
            "median": median(values), "min": values[0], "p90": p90, "max": values[-1]}


def by_turn(events, kind):
    result = {}
    for event in events:
        if event.get("type") == kind:
            if event["turn"] in result:
                raise ValueError(f"Duplicate {kind} event for turn {event['turn']}")
            result[event["turn"]] = event
    return result


def read_records(path):
    with tarfile.open(path) as archive:
        checksums = json.load(archive.extractfile("sha256.json"))
        records = {}
        for name, digest in checksums.items():
            content = archive.extractfile(name).read()
            if hashlib.sha256(content).hexdigest() != digest:
                raise ValueError(f"Archive checksum mismatch: {name}")
            records[name] = content
    return records


def request_end_index(content):
    result = {}
    for line in content.decode().splitlines():
        event = json.loads(line).get("event", {})
        if event.get("event_type") == "request_end":
            ident = event["request"]["request_id"]
            if ident in result:
                raise ValueError("Duplicate server request ID")
            result[ident] = event["request"]
    return result


def replay_eligibility(events, requests, inference, assistants, run, verified_turns, valid_pairs):
    reasons = []
    numbers = sorted(requests)
    if not numbers or numbers != list(range(1, len(numbers) + 1)):
        reasons.append("Requests must cover consecutive turns starting at one")
    if set(requests) != set(inference) or set(requests) != set(assistants):
        reasons.append("Request, inference, and assistant turns must agree")
    if verified_turns != set(requests) or len(valid_pairs) != max(0, len(requests) - 1):
        reasons.append("Every request and continuation needs verified rendering")
    if any(e.get("response_status") != "completed" or e.get("finish_reason") not in ("stop", "tool_calls")
           or e.get("output_limit_hit") is not False for e in inference.values()):
        reasons.append("Every response must have completed within its token budget")
    summaries = [e for e in events if e.get("type") == "task_summary"]
    tools = [e for e in events if e.get("type") == "tool"]
    if (len(summaries) != 1 or summaries[0].get("stop_reason") != "no_tool_calls"
            or summaries[0].get("agent_completed") is not True
            or summaries[0].get("model_calls") != len(requests)
            or summaries[0].get("tool_calls") != len(tools)):
        reasons.append("A complete terminal summary must reconcile model and tool calls")
    if (numbers and (assistants.get(numbers[-1], {}).get("message", {}).get("tool_calls")
                     or inference.get(numbers[-1], {}).get("finish_reason") != "stop")):
        reasons.append("The final assistant response must end without tool calls")
    if any(e.get("turn") not in numbers[:-1] for e in tools):
        reasons.append("Tool events must belong to a recorded continuation")
    if run.get("status") != "finished" or run.get("exception_type") is not None or run.get("harbor_returncode") != 0:
        reasons.append("The source trial did not finish cleanly")
    return reasons


def transition(renderer, current, assistant, following, inference, next_inference,
               tools, *, block_size=16):
    if inference.get("response_status") != "completed" or inference.get("finish_reason") not in ("stop", "tool_calls"):
        raise ValueError("Incomplete response")
    if type(inference.get("output_tokens")) is not int or inference["output_tokens"] < 0:
        raise ValueError("Invalid prior output token count")
    request, next_request = current["request"], following["request"]
    message = assistant["message"]
    audit.check_followup_contract(request, message, next_request)
    initial_ids = renderer.tokens(renderer.render(request, request["messages"], generation=True))
    next_ids = renderer.tokens(renderer.render(next_request, next_request["messages"], generation=True))
    candidate = renderer.candidate(request, message, block_size=block_size)["prepared_token_ids"]
    if next_ids[:len(candidate)] != candidate:
        raise ValueError("Prepared candidate is not the next request's exact prefix")
    bounds = audit.decode_prefix_bounds(initial_ids, next_ids, inference["output_tokens"])
    lower = bounds["lower_tokens"] // block_size * block_size
    upper = bounds["upper_tokens"] // block_size * block_size
    initial_full = len(initial_ids) // block_size * block_size
    shared = audit.common_prefix(initial_ids, next_ids)
    input_shared = shared // block_size * block_size
    duration = sum(t["duration_ms"] for t in tools)
    if any(not math.isfinite(t["duration_ms"]) or t["duration_ms"] < 0 for t in tools):
        raise ValueError("Invalid tool duration")
    calls = message.get("tool_calls", [])
    if [t["call_id"] for t in tools] != [call["id"] for call in calls]:
        raise ValueError("Recorded tools do not match the completed assistant calls")
    if not tools:
        raise ValueError("Continuation has no recorded tool wait")
    gap = following["elapsed_ms"] - assistant["elapsed_ms"]
    if not math.isfinite(gap) or gap < 0:
        raise ValueError("Invalid response-to-request interval")
    cached = next_inference["cached_input_tokens"]
    if type(cached) is not int or not 0 <= cached <= len(next_ids):
        raise ValueError("Invalid recorded cache count")
    return {
        "from_turn": current["turn"], "to_turn": following["turn"], "status": "verified",
        "prior_prompt_tokens": len(initial_ids), "next_prompt_tokens": len(next_ids),
        "prior_output_tokens": inference["output_tokens"],
        "prior_input_full_block_tokens": initial_full,
        "shared_input_full_block_tokens": input_shared,
        "prior_input_full_block_tokens_lost": initial_full - input_shared,
        "initial_to_next_common_tokens": shared,
        "mismatch_before_sampled_output": shared < len(initial_ids),
        "ordinary_decode_reusable_tokens_lower": lower,
        "ordinary_decode_reusable_tokens_upper": upper,
        "raw_sampled_output_ids_available": False,
        "prepared_prefix_tokens": len(candidate),
        "additional_preparable_tokens_lower": max(0, len(candidate) - upper),
        "additional_preparable_tokens_upper": max(0, len(candidate) - lower),
        "tokens_after_preparable_prefix": len(next_ids) - len(candidate),
        "tool_wait_ms": duration,
        "response_to_next_request_ms": gap,
        "tool_names": [t["tool"] for t in tools],
        "recorded_next_cached_tokens": cached,
        "recorded_cache_exceeds_previous_input_prefix": cached > input_shared,
    }


def survey(tokenizer, archive_path=ARCHIVE, block_size=16):
    if type(block_size) is not int or block_size <= 0:
        raise ValueError("block_size must be a positive integer")
    renderer = audit.QwenRenderer(Path(tokenizer))
    records = read_records(archive_path)
    manifest = json.loads(records["pilot/manifest.json"])
    deployment = manifest["deployment"]
    if (manifest["model"] != audit.MODEL or deployment["model_revision"] != audit.REVISION
            or deployment.get("thinking_mode") != "disabled"):
        raise ValueError("The archive differs from the renderer's pinned deployment")
    server = request_end_index(records["server/frontend-trace.jsonl"])
    run_rows, request_rows, pairs, workflows, skipped = [], [], [], [], []
    for run in manifest["runs"]:
        trace_bytes = records["pilot/" + run["trace"]]
        if hashlib.sha256(trace_bytes).hexdigest() != run["trace_sha256"]:
            raise ValueError("Pilot trace differs from its manifest")
        events = [json.loads(line) for line in trace_bytes.decode().splitlines() if line.strip()]
        requests, inference, assistants = (by_turn(events, kind) for kind in ("request", "inference", "assistant_message"))
        configs = [e for e in events if e.get("type") == "run_config"]
        if len(configs) != 1 or configs[0].get("capture_requests") is not True:
            raise ValueError("Trace does not contain a captured run")
        verified_turns = set()
        run_requests, run_pairs = [], []
        for number, request_event in sorted(requests.items()):
            row = {"run_id": run["run_id"], "turn": number, "status": "unverifiable"}
            try:
                inf = inference[number]
                ident = inf["completion_id"].removeprefix("chatcmpl-")
                recorded = server[ident]
                request = request_event["request"]
                ids = renderer.tokens(renderer.render(request, request["messages"], generation=True))
                check = audit.validate_server_prompt(ids, recorded)
                if check["status"] != "verified" or check["trace_block_size"] != block_size:
                    raise ValueError("Rendered token fingerprints differ from the server")
                if len(ids) != inf["input_tokens"] or recorded["input_tokens"] != inf["input_tokens"]:
                    raise ValueError("Client and server input counts differ")
                row.update(status="verified", input_tokens=len(ids), output_tokens=inf["output_tokens"],
                           recorded_cached_tokens=inf["cached_input_tokens"], compared_hashes=check["compared_hashes"],
                           server_request_id=ident,
                           server_reported_prefill_ms=recorded.get("prefill_time_ms"),
                           server_reported_prefill_wait_ms=recorded.get("prefill_wait_time_ms"),
                           server_reported_total_ms=recorded.get("total_time_ms"))
                verified_turns.add(number)
            except (KeyError, ValueError, TypeError) as error:
                row["reason"] = str(error)
            request_rows.append(row)
            run_requests.append(row)
        numbers = sorted(requests)
        for before, after in zip(numbers, numbers[1:]):
            row = {"run_id": run["run_id"], "from_turn": before, "to_turn": after, "status": "unverifiable"}
            try:
                if after != before + 1 or before not in verified_turns or after not in verified_turns:
                    raise ValueError("Both adjacent requests require verified rendering")
                tools = [e for e in events if e.get("type") == "tool" and e["turn"] == before]
                row.update(transition(renderer, requests[before], assistants[before], requests[after],
                                      inference[before], inference[after], tools, block_size=block_size))
            except (KeyError, ValueError, TypeError) as error:
                row["reason"] = str(error)
            pairs.append(row)
            run_pairs.append(row)
        valid = [p for p in run_pairs if p["status"] == "verified"]
        run_rows.append({"run_id": run["run_id"], "task": run["task"], "source_condition": run["condition"],
                         "reward": run["reward"], "exception_type": run["exception_type"],
                         "requests": len(run_requests), "verified_requests": len(verified_turns),
                         "continuations": len(run_pairs), "verified_continuations": len(valid),
                         "additional_preparable_tokens_lower": sum(p["additional_preparable_tokens_lower"] for p in valid),
                         "additional_preparable_tokens_upper": sum(p["additional_preparable_tokens_upper"] for p in valid),
                         "tool_wait_ms": sum(p["tool_wait_ms"] for p in valid),
                         "trace_sha256": run["trace_sha256"]})
        reasons = replay_eligibility(events, requests, inference, assistants, run, verified_turns, valid)
        if reasons:
            skipped.append({"run_id": run["run_id"], "reasons": reasons})
        else:
            turns = []
            for number in numbers:
                request = {key: value for key, value in requests[number]["request"].items() if key != "model"}
                wait = sum(e["duration_ms"] for e in events if e.get("type") == "tool" and e["turn"] == number - 1)
                turns.append({"request": request, "wait_ms": wait, "expected_message": assistants[number]["message"]})
            workflows.append({"id": run["run_id"], "arrival_ms": 0, "turns": turns})
    valid = [p for p in pairs if p["status"] == "verified"]
    request_ok = [r for r in request_rows if r["status"] == "verified"]
    fields = ("prior_prompt_tokens", "next_prompt_tokens", "prior_output_tokens", "tool_wait_ms",
              "additional_preparable_tokens_lower", "additional_preparable_tokens_upper",
              "tokens_after_preparable_prefix", "prior_input_full_block_tokens_lost")
    first = [r for r in request_ok if r["turn"] == 1]
    initial = sum(p["prior_input_full_block_tokens"] for p in valid)
    shared = sum(p["shared_input_full_block_tokens"] for p in valid)
    next_total = sum(p["next_prompt_tokens"] for p in valid)
    report = {
        "schema_version": 1, "evidence": "retrospective_token_continuity_survey",
        "new_model_calls": 0, "new_gpu_measurements": False,
        "source_archive_sha256": hashlib.sha256(Path(archive_path).read_bytes()).hexdigest(),
        "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (Path(__file__), ROOT / "audit.py")},
        "package_versions": {name: version(name) for name in ("Jinja2", "tokenizers", "xxhash")},
        "model": audit.MODEL, "model_revision": audit.REVISION, "tokenizer_sha256": audit.TOKENIZER_SHA256,
        "template_sha256": audit.TEMPLATE_SHA256, "block_size": block_size,
        "coverage": {"runs": len(run_rows), "tasks": sorted({r["task"] for r in run_rows}),
                     "requests": len(request_rows), "verified_requests": len(request_ok),
                     "continuations": len(pairs), "verified_continuations": len(valid),
                     "compared_server_hashes": sum(r["compared_hashes"] for r in request_ok)},
        "replay_export": {"complete": bool(run_rows) and not skipped,
                          "exported_workflows": len(workflows), "skipped": skipped},
        "summary": {key: describe(p[key] for p in valid) for key in fields},
        "input_continuity": {"prior_complete_block_tokens": initial, "shared_complete_block_tokens": shared,
                             "preserved_fraction": shared / initial if initial else None,
                             "mismatches_before_sampled_output": sum(p["mismatch_before_sampled_output"] for p in valid)},
        "candidate_opportunity": {"additional_tokens_lower": sum(p["additional_preparable_tokens_lower"] for p in valid),
                                  "additional_tokens_upper": sum(p["additional_preparable_tokens_upper"] for p in valid),
                                  "total_continuation_input_tokens": next_total,
                                  "fraction_of_continuation_inputs_lower": sum(p["additional_preparable_tokens_lower"] for p in valid) / next_total if next_total else None},
        "recorded_cache": {"initial_requests_with_hits": sum(r["recorded_cached_tokens"] > 0 for r in first),
                           "initial_requests": len(first),
                           "continuations_above_previous_input_prefix": sum(p["recorded_cache_exceeds_previous_input_prefix"] for p in valid)},
        "server_reported_times": {key: describe(r[key] for r in request_ok if r[key] is not None)
                                  for key in ("server_reported_prefill_ms", "server_reported_prefill_wait_ms", "server_reported_total_ms")},
        "limits": ["Ten trajectories from one easy synthetic coding task; no across-task generalization.",
                   "The renderer is checked against saved server input fingerprints, including partial final blocks.",
                   "Raw generated IDs were not saved. Decode reuse is bounded; an input mismatch makes the bound exact.",
                   "Preparable blocks describe compatible content, not its actual residency or saved GPU work.",
                   "Tokens after the candidate include tool content, message framing, and block-rounding slack.",
                   "Cache carried between trials; source off/on groups do not establish a preparation effect here.",
                   "Recorded timing fields are per-request wall times; no counterfactual bound or GPU kernel time.",
                   "Tool durations do not reveal when output bytes became available.",
                   "Summaries are descriptive; p90 uses linear interpolation; no confidence intervals."],
        "runs": run_rows, "requests": request_rows, "transitions": pairs,
    }
    workload = {"schema_version": 1, "source_model": audit.MODEL,
                "source_archive_sha256": report["source_archive_sha256"],
                "selection": "Every verified complete pilot trajectory; source condition is provenance only.",
                "workflows": workflows}
    report["replay_export"]["workload_sha256"] = hashlib.sha256(
        (json.dumps(workload, indent=2) + "\n").encode()).hexdigest()
    return report, workload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--archive", type=Path, default=ARCHIVE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--export-workload", type=Path)
    args = parser.parse_args()
    if args.output.exists() or (args.export_workload and args.export_workload.exists()):
        parser.error("Choose new output paths; existing records are preserved")
    report, workload = survey(args.tokenizer, args.archive)
    with args.output.open("x") as file:
        file.write(json.dumps(report, indent=2) + "\n")
    if args.export_workload:
        if not report["replay_export"]["complete"]:
            parser.error("Cannot export a subset as the complete workload; inspect the report")
        with args.export_workload.open("x") as file:
            file.write(json.dumps(workload, indent=2) + "\n")
    print(json.dumps({key: report[key] for key in ("coverage", "input_continuity", "candidate_opportunity")}, indent=2))


if __name__ == "__main__":
    main()
