#!/usr/bin/env python3
"""Report prefix reuse from explicitly linked client captures and backend IDs."""

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path


CAUSES = {"rendering", "reasoning_round_trip", "tool_call_round_trip", "tool_schema_order",
          "harness_edits", "eviction", "engine_boundary", "other"}


def count(value):
    return value if type(value) is int and value >= 0 else None


def sse_events(raw):
    text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    if text.split("\n\n")[-1].strip():
        raise ValueError("Unterminated SSE event")
    events = []
    for block in text.split("\n\n")[:-1]:
        lines = [line[5:].removeprefix(" ") for line in block.split("\n") if line.startswith("data:")]
        if not lines:
            continue
        data = "\n".join(lines)
        if data == "[DONE]":
            continue
        event = json.loads(data)
        if not isinstance(event, dict):
            raise ValueError("SSE data must be a JSON object")
        events.append(event)
    return events


def response_usage(raw, protocol):
    result = {"terminal_status": None, "stop_reason": None, "input_tokens": None,
              "cached_tokens": None, "uncached_tokens": None, "cache_write_tokens": None,
              "output_tokens": None, "warnings": []}
    try:
        events = sse_events(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        result["warnings"].append("SSE parse failure: " + str(error))
        return result
    if any(event.get("type") == "error" for event in events):
        result["terminal_status"] = "failed"
        result["warnings"].append("Server returned an error event")
        return result
    if protocol == "responses":
        terminals = [event for event in events if event.get("type") in
                     ("response.completed", "response.incomplete", "response.failed")]
        if len(terminals) != 1:
            result["warnings"].append("Expected one final Responses event")
            return result
        final = terminals[0]
        response = final.get("response") or {}
        result["terminal_status"] = final["type"].split(".", 1)[1]
        result["stop_reason"] = (response.get("incomplete_details") or {}).get("reason")
        usage = response.get("usage") or {}
        result.update(input_tokens=count(usage.get("input_tokens")),
                      cached_tokens=count((usage.get("input_tokens_details") or {}).get("cached_tokens")),
                      output_tokens=count(usage.get("output_tokens")))
    elif protocol == "anthropic":
        stops = [event for event in events if event.get("type") == "message_stop"]
        deltas = [event for event in events if event.get("type") == "message_delta"]
        if len(stops) != 1 or not deltas or events.index(deltas[-1]) > events.index(stops[0]):
            result["warnings"].append("Missing final Anthropic message_delta/message_stop pair")
            return result
        final = deltas[-1]
        usage = final.get("usage") or {}
        result.update(terminal_status="completed", stop_reason=(final.get("delta") or {}).get("stop_reason"),
                      uncached_tokens=count(usage.get("input_tokens")),
                      cached_tokens=count(usage.get("cache_read_input_tokens")),
                      cache_write_tokens=count(usage.get("cache_creation_input_tokens")),
                      output_tokens=count(usage.get("output_tokens")))
        pieces = [result[key] for key in ("uncached_tokens", "cached_tokens", "cache_write_tokens")]
        if all(piece is not None for piece in pieces):
            result["input_tokens"] = sum(pieces)
        if result["cached_tokens"] is None:
            result["warnings"].append("Final Anthropic cache reads absent; stock omits both zero and unknown")
    else:
        raise ValueError("protocol must be anthropic or responses")
    if result["input_tokens"] is None:
        result["warnings"].append("Final total input usage unknown")
    if result["cached_tokens"] is None:
        result["warnings"].append("Final cached-token usage unknown")
    return result


def lcp(left, right):
    for position, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return position
    return min(len(left), len(right))


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def hashed_json(path, expected):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("Evidence hash mismatch: " + str(path))
    return json.loads(raw)


def json_pointer(value, pointer):
    if not pointer:
        return value
    if not pointer.startswith("/"):
        raise ValueError("JSON pointer must start with /")
    for component in pointer[1:].split("/"):
        component = component.replace("~1", "/").replace("~0", "~")
        value = value[int(component)] if isinstance(value, list) else value[component]
    return value


def load_backend(spec, base):
    if not spec.get("request_id") or not spec.get("worker_id") or not spec.get("epoch"):
        raise ValueError("Backend mapping needs request_id, worker_id and epoch")
    value = hashed_json(base / spec["path"], spec["sha256"])
    tokens = json_pointer(value, spec.get("token_ids_pointer", ""))
    if not isinstance(tokens, list) or any(count(token) is None for token in tokens):
        raise ValueError("Backend IDs must be an array of nonnegative integers")
    return tokens


def resolve_causes(start, end, supplied, default_explanation):
    entries = sorted(supplied, key=lambda entry: entry["start"])
    result, cursor = [], start
    for entry in entries:
        left, right = entry["start"], entry["end"]
        if type(left) is not int or type(right) is not int or not cursor <= left < right <= end:
            raise ValueError("Cause ranges overlap or exceed their measured interval")
        if entry.get("cause") not in CAUSES or not entry.get("explanation"):
            raise ValueError("Cause needs a supported label and an explanation")
        if entry["cause"] != "other" and not entry.get("evidence"):
            raise ValueError("An attributed cause needs explicit evidence")
        if left > cursor:
            result.append({"start": cursor, "end": left, "cause": "other",
                           "explanation": default_explanation, "evidence": []})
        result.append(dict(entry))
        cursor = right
    if cursor < end:
        result.append({"start": cursor, "end": end, "cause": "other",
                       "explanation": default_explanation, "evidence": []})
    return result


def aggregate(rows):
    usable = [row for row in rows if row["usable"]]
    total = sum(row["input_tokens"] for row in usable)
    cached = sum(row["cached_tokens"] for row in usable)
    ideal = sum(row["ideal_tokens"] for row in usable)
    result = {"requests": len(rows), "usable_requests": len(usable),
              "excluded_request_ids": [row["id"] for row in rows if not row["usable"]],
              "input_tokens": total, "cached_tokens": cached, "ideal_tokens": ideal,
              "actual_reuse": ratio(cached, total), "ideal_reuse": ratio(ideal, total),
              "gap": ratio(ideal - cached, total), "cached_over_ideal": ratio(cached, ideal)}
    if usable and all(row["eligible_ideal_tokens"] is not None for row in usable):
        eligible = sum(row["eligible_ideal_tokens"] for row in usable)
        result.update(eligible_ideal_tokens=eligible, eligible_gap=ratio(eligible - cached, total))
    else:
        result.update(eligible_ideal_tokens=None, eligible_gap=None)
    return result


def build_report(mapping, base):
    if mapping.get("block_size") != 16:
        raise ValueError("This frozen study uses block_size=16")
    backend_uses, ids, sessions, all_rows = set(), set(), [], []
    causes = mapping.get("causes", {})
    engine_rule = mapping.get("engine_boundary")
    if engine_rule and (engine_rule.get("rule") != "last_token_recomputed" or not engine_rule.get("evidence")):
        raise ValueError("Engine boundary requires last_token_recomputed and source evidence")
    for session in mapping["sessions"]:
        history, rows, previous_turn = [], [], None
        for ordinal, entry in enumerate(session["requests"]):
            request_id = entry["id"]
            if request_id in ids:
                raise ValueError("Request IDs must be unique")
            ids.add(request_id)
            turn = entry["user_turn"]
            if type(turn) is not int or turn < 1 or not entry.get("conversation_id"):
                raise ValueError("Every request needs a user_turn and conversation_id")
            if not entry.get("auxiliary") and previous_turn is not None and turn < previous_turn:
                raise ValueError("Main-session user turns must stay in order")
            folder = base / entry["capture"]
            meta = json.loads((folder / "metadata.json").read_text())
            response = (folder / "response.body").read_bytes() if (folder / "response.body").exists() else b""
            body = (folder / "request.body").read_bytes() if (folder / "request.body").exists() else b""
            usage = response_usage(response, session["protocol"])
            warnings = list(usage.pop("warnings"))
            row = {"id": request_id, "session": session["id"], "ordinal": ordinal,
                   "conversation_id": entry["conversation_id"], "user_turn": turn,
                   "capture": entry["capture"], "usage": usage, "warnings": warnings,
                   "link_evidence": entry.get("link_evidence", []),
                   "http_status": meta.get("response_status"), "capture_complete": meta.get("complete"),
                   "started_monotonic_ns": meta.get("started_monotonic_ns"),
                   "ended_monotonic_ns": meta.get("ended_monotonic_ns"),
                   "evidence_hashes": {"metadata": hashlib.sha256((folder / "metadata.json").read_bytes()).hexdigest(),
                                       "request": hashlib.sha256(body).hexdigest(),
                                       "response": hashlib.sha256(response).hexdigest()},
                   "input_tokens": None, "cached_tokens": usage["cached_tokens"],
                   "ideal_tokens": None, "eligible_ideal_tokens": None,
                   "actual_reuse": None, "ideal_reuse": None, "gap": None,
                   "eligible_gap": None, "reference_request_ids": [], "usable": False,
                   "compatible_misses": [], "history_rewrites": []}
            row["group"] = ("auxiliary" if entry.get("auxiliary") else "initial_cold" if previous_turn is None else
                            "cross_turn" if previous_turn != turn else "within_turn")
            if not entry.get("auxiliary"):
                previous_turn = turn
            if not meta.get("complete") or meta.get("response_status") != 200:
                warnings.append("HTTP capture incomplete or unsuccessful")
            if (count(meta.get("started_monotonic_ns")) is None or count(meta.get("ended_monotonic_ns")) is None
                    or meta["ended_monotonic_ns"] < meta["started_monotonic_ns"]):
                warnings.append("Capture clock interval invalid")
            for name, raw in (("request", body), ("response", response)):
                if not raw or meta.get(name + "_sha256") != hashlib.sha256(raw).hexdigest():
                    warnings.append(name + " body hash missing or mismatched")
            tokens, backend = None, entry.get("backend")
            if backend and entry.get("link_evidence"):
                try:
                    tokens = load_backend(backend, base)
                    key = (backend["worker_id"], backend["epoch"], backend["request_id"])
                    if key in backend_uses:
                        raise ValueError("Backend request mapped more than once")
                    backend_uses.add(key)
                except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
                    warnings.append("Backend evidence rejected: " + str(error))
                    tokens = None
            else:
                warnings.append("No explicit backend/request mapping with link evidence")
            if tokens is not None:
                row["input_tokens"] = len(tokens)
                if not tokens:
                    warnings.append("Backend input IDs are empty")
                row["backend"] = backend
                earlier = [item for item in history if item["conversation_id"] == entry["conversation_id"]
                           and item["worker_id"] == backend["worker_id"] and item["epoch"] == backend["epoch"]
                           and item["ended_monotonic_ns"] <= (count(meta.get("started_monotonic_ns")) or -1)]
                raw_ideal = max((lcp(tokens, item["tokens"]) for item in earlier), default=0)
                row["reference_request_ids"] = [item["id"] for item in earlier]
                row["ideal_tokens"] = 16 * (raw_ideal // 16)
                if engine_rule:
                    row["eligible_ideal_tokens"] = min(row["ideal_tokens"], 16 * (max(0, len(tokens) - 1) // 16))
                row["ideal_reuse"] = ratio(row["ideal_tokens"], len(tokens))
                backend_usage = entry.get("backend_usage")
                if backend_usage:
                    if not backend_usage.get("evidence"):
                        raise ValueError("Backend usage needs explicit source evidence")
                    backend_input = count(backend_usage.get("input_tokens"))
                    backend_cached = count(backend_usage.get("cached_tokens"))
                    if backend_input != len(tokens) or backend_cached is None or backend_cached > len(tokens):
                        warnings.append("Mapped backend usage invalid or disagrees with input IDs")
                    else:
                        for field, value in (("input_tokens", backend_input), ("cached_tokens", backend_cached)):
                            if usage[field] is not None and usage[field] != value:
                                warnings.append("Frontend/backend " + field + " disagreement")
                        if session["protocol"] == "anthropic" and usage["uncached_tokens"] is not None:
                            write = usage["cache_write_tokens"]
                            if write is None or usage["uncached_tokens"] + backend_cached + write != backend_input:
                                warnings.append("Anthropic breakdown disagrees with mapped backend usage")
                        row["cached_tokens"] = backend_cached
                        row["backend_usage"] = backend_usage
                input_usage = backend_usage.get("input_tokens") if backend_usage else usage["input_tokens"]
                if input_usage is None:
                    warnings.append("No final total input usage to cross-check backend IDs")
                elif input_usage != len(tokens):
                    warnings.append("Final input usage disagrees with backend IDs")
                if row["cached_tokens"] is None:
                    warnings.append("No measured cached-token count")
                elif row["cached_tokens"] > len(tokens):
                    warnings.append("Cached-token count exceeds input IDs")
                completed = usage["terminal_status"] in ("completed", "incomplete") and meta.get("complete")
                if not completed:
                    warnings.append("Request lacks a final model response")
                diagnostic = {"Final total input usage unknown", "Final cached-token usage unknown",
                              "Final Anthropic cache reads absent; stock omits both zero and unknown"}
                row["usable"] = not any(warning not in diagnostic for warning in warnings)
                if row["usable"]:
                    cached, ideal, size = row["cached_tokens"], row["ideal_tokens"], len(tokens)
                    row.update(actual_reuse=ratio(cached, size), gap=ratio(ideal - cached, size))
                    eligible = row["eligible_ideal_tokens"]
                    if eligible is not None:
                        row["eligible_gap"] = ratio(eligible - cached, size)
                    end = eligible if eligible is not None else ideal
                    supplied = causes.get(request_id, {}).get("compatible_misses", [])
                    row["compatible_misses"] = resolve_causes(min(cached, end), end, supplied,
                        "No evidence assigns this compatible-prefix miss to a cause" +
                        ("; engine boundary unverified" if eligible is None else ""))
                broken_reference = any("hash" in warning or "disagree" in warning or "clock" in warning
                                       or "invalid" in warning or "empty" in warning
                                       for warning in warnings)
                if completed and meta.get("response_status") == 200 and not broken_reference:
                    history.append({"id": request_id, "tokens": tokens, "conversation_id": entry["conversation_id"],
                                    "worker_id": backend["worker_id"], "epoch": backend["epoch"],
                                    "ended_monotonic_ns": meta.get("ended_monotonic_ns", float("inf"))})
            row["history_rewrites"] = causes.get(request_id, {}).get("history_rewrites", [])
            grouped_rewrites = defaultdict(list)
            for cause in row["history_rewrites"]:
                reference = next((item for item in history if item["id"] == cause.get("reference_request_id")
                                  and item["conversation_id"] == entry["conversation_id"]
                                  and item["id"] != request_id), None)
                if not reference:
                    raise ValueError("History rewrite needs an earlier completed reference request")
                grouped_rewrites[reference["id"]].append(cause)
                if cause.get("end", 0) > len(reference["tokens"]):
                    raise ValueError("History rewrite exceeds reference input")
            for entries in grouped_rewrites.values():
                resolve_causes(0, max(item["end"] for item in entries), entries, "unassigned")
            rows.append(row)
        groups = {name: aggregate([row for row in rows if row["group"] == name])
                  for name in ("initial_cold", "within_turn", "cross_turn", "auxiliary")}
        sessions.append({"id": session["id"], "protocol": session["protocol"],
                         "all": aggregate(rows), "main": aggregate([row for row in rows if row["group"] != "auxiliary"]),
                         "groups": groups,
                         "completed_followup_turns": sorted({row["user_turn"] for row in rows
                                                            if row["usable"] and row["user_turn"] > 1})})
        all_rows.extend(rows)
    if set(causes) - ids:
        raise ValueError("Cause ledger names an unmapped request")
    rankings = {}
    for ledger in ("compatible_misses", "history_rewrites"):
        totals = defaultdict(int)
        for row in all_rows:
            for entry in row[ledger]:
                totals[entry["cause"]] += entry["end"] - entry["start"]
        rankings[ledger] = dict(sorted(totals.items(), key=lambda pair: (-pair[1], pair[0])))
    return {"schema_version": 1, "block_size": 16, "engine_boundary": engine_rule,
            "sessions": sessions, "requests": all_rows,
            "cause_rankings": rankings,
            "limits": ["Input-only ideal; earlier generated IDs are excluded.",
                       "Only explicitly linked, completed requests in the same conversation and worker epoch form references.",
                       "Unknown or inconsistent usage remains visible and is excluded from paired aggregates.",
                       "A final output-limit response with measured usage stays in the analysis; failed or unfinished streams do not form references.",
                       "History-rewrite counts are supplied diagnostic exposures; they are separate from compatible misses.",
                       "This baseline report makes no parity verdict or causal claim without supporting evidence."]}


def markdown(report):
    lines = ["# Parity trace report", "", "| Request | Group | Input | Cached | Ideal | Gap | Usable |",
             "| --- | --- | ---: | ---: | ---: | ---: | --- |"]
    for row in report["requests"]:
        values = [row[key] for key in ("id", "group", "input_tokens", "cached_tokens", "ideal_tokens")]
        values += [f"{100 * row['gap']:.2f}%" if row["gap"] is not None else None, row["usable"]]
        lines.append("| " + " | ".join("N/A" if value is None else str(value) for value in values) + " |")
    for session in report["sessions"]:
        lines += ["", "## " + session["id"], "", "| Group | Usable / recorded | Actual reuse | Ideal reuse | Gap | Cached / ideal |",
                  "| --- | ---: | ---: | ---: | ---: | ---: |"]
        for name, stats in [("all", session["all"]), ("main", session["main"]), *session["groups"].items()]:
            values = [name, f"{stats['usable_requests']} / {stats['requests']}"]
            values += ["N/A" if stats[key] is None else f"{stats[key] * 100:.2f}%"
                       for key in ("actual_reuse", "ideal_reuse", "gap", "cached_over_ideal")]
            lines.append("| " + " | ".join(values) + " |")
    lines += ["", "## Evidence limits", ""] + ["- " + text for text in report["limits"]]
    for row in report["requests"]:
        if row["warnings"]:
            lines.append("- " + row["id"] + ": " + "; ".join(row["warnings"]))
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    mapping = json.loads(args.mapping.read_text())
    report = build_report(mapping, args.mapping.resolve().parent)
    args.output.mkdir(parents=True, exist_ok=False)
    report["mapping_sha256"] = hashlib.sha256(args.mapping.read_bytes()).hexdigest()
    (args.output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    (args.output / "report.md").write_text(markdown(report))


if __name__ == "__main__":
    main()
