"""Measure observed concurrent request windows without inferring interference."""

import argparse
import json
import math
from pathlib import Path
import sys

from validate_capture import validate_capture


def union(windows):
    merged = []
    for start, end in sorted(windows):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def overlap(window, others):
    if window is None:
        return None
    start, end = window
    return sum(max(0, min(end, b) - max(start, a)) for a, b in union(others))


def native_window(result):
    stats = (result or {}).get("engine_stats") or {}
    start, end = stats.get("scheduled_ts"), stats.get("last_token_ts")
    if not all(type(value) in (int, float) and math.isfinite(value) and value > 0 for value in (start, end)) or end < start:
        return None
    return (int(start * 1e9), int(end * 1e9))


def request_window(entry, result):
    start = (entry or {}).get("worker_monotonic_ns")
    end = (result or {}).get("worker_monotonic_ns")
    if type(start) is not int or type(end) is not int or end < start:
        return None
    return start, end


def trial_overlap(path, entries, results, target):
    manifest = json.loads((path / "manifest.json").read_text())
    sessions = [json.loads(p.read_text()) for p in path.glob("sessions/*.json")]
    task_by_session = {row["session_id"]: row["task_id"] for row in sessions}
    rows = []
    clients = [(row, "normal", row["session_id"]) for file in path.glob("*-requests.json")
               for row in json.loads(file.read_text())]
    clients += [(json.loads(file.read_text()), "preparation", file.parent.name)
                for file in path.glob("preparations/*/*.json")]
    for client, kind, session_id in clients:
        rid = client["client_request_id"]
        task = task_by_session.get(session_id)
        role = ("target" if task == target else "background") if task is not None else "unknown"
        if kind == "preparation":
            role += "_preparation"
        entry, result = entries.get(rid), results.get(rid)
        rows.append({"request_id": rid, "session_id": session_id, "role": role,
                     "client_status": client.get("status"), "client_error": client.get("error"),
                     "response_issue": client.get("response_issue"),
                     "backend_terminal": (result or {}).get("terminal"),
                     "native_window_ns": native_window(result),
                     "request_lifetime_ns": request_window(entry, result)})
    summary = {}
    background = [row for row in rows if row["role"] == "background"]
    for metric, field in (("native", "native_window_ns"), ("lifetime", "request_lifetime_ns")):
        windows = [row[field] for row in background if row[field] is not None]
        known_background = len(windows)
        for row in rows:
            value = overlap(row[field], windows)
            row[metric + "_background_overlap_ms"] = value / 1e6 if value is not None else None
        for role in ("target", "target_preparation"):
            selected = [row for row in rows if row["role"] == role]
            known = [row for row in selected if row[field] is not None]
            duration = sum(row[field][1] - row[field][0] for row in known) / 1e6
            observed_overlap = sum(row[metric + "_background_overlap_ms"] for row in known)
            summary[role + "_" + metric] = {
                "request_count": len(selected), "known_window_count": len(known),
                "missing_window_count": len(selected) - len(known),
                "background_request_count": len(background),
                "background_known_window_count": known_background,
                "requests_with_observed_overlap": sum(row[metric + "_background_overlap_ms"] > 0 for row in known),
                "summed_known_window_ms": duration, "summed_observed_overlap_ms": observed_overlap,
                "observed_overlap_fraction": observed_overlap / duration if duration else None,
                "all_background_windows_known": known_background == len(background),
            }
    return {"trial": path.name, "condition": manifest["condition"], "background_configured": manifest["background"],
            "trial_id": manifest["trial_id"], "rows": rows, "summary": summary,
            "sessions": [{key: row.get(key) for key in ("session_id", "task_id", "outcome", "turns", "model_calls", "tool_calls", "foreground_elapsed_s")}
                         for row in sessions]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target-task", default="psf__requests-6028")
    args = parser.parse_args()
    validation = validate_capture(args.engine)
    events = [json.loads(line) for line in args.engine.read_text().splitlines()]
    entries = {row["request_id"]: row for row in events if row["kind"] == "engine_enter"}
    results = {row["request_id"]: row for row in events if row["kind"] == "engine_result"}
    report = {"worker_capture": validation, "cohort": str(args.runs),
              "clock_domains": {"native": "EngineCore monotonic seconds, converted to nanoseconds; native/native comparison only within this single-engine run", "lifetime": "Capture-worker Python monotonic nanoseconds; lifetime/lifetime comparison only"},
              "meaning": "Native windows span first scheduling through last observed token. They include decode gaps and do not prove simultaneous GPU kernels or causal interference. Lifetime windows cover the Python request only; abort acknowledgement does not establish when GPU execution stopped. Missing native windows remain unknown; reported overlap uses only observed background windows. The two timestamp domains are never compared or subtracted.",
              "trials": [trial_overlap(p.parent, entries, results, args.target_task) for p in sorted(args.runs.glob("*/manifest.json"))]}
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"cohort": str(args.runs), "trials": len(report["trials"]), "output": str(args.output)}))


if __name__ == "__main__":
    main()
