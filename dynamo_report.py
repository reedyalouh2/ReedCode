"""Summarize Dynamo pilot and replay records without dropping failed attempts."""

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
from statistics import mean

from reporting import analyze_trace


def checked_json(path, digest):
    data = path.read_bytes()
    if not digest or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError(f"Checksum mismatch: {path.name}")
    return json.loads(data)


def total(values):
    values = list(values)
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
        return None
    return sum(values)


def percentile90(values):
    return sorted(values)[math.ceil(0.9 * len(values)) - 1] if values else None


def epoch_values(epoch):
    workflows = epoch["workflows"]
    calls = [c for w in workflows for c in w["calls"]]
    post_tool = [c for c in calls if c["turn"] > 1]
    timings = [(c.get("stream_timing") or {}).get("first_output_ms") for c in post_tool]
    usage = [c.get("usage") for c in calls] + [w.get("session_final", {}).get("usage") for w in workflows]
    complete_usage = all(c["status"] == "completed" for c in calls) and all(isinstance(u, dict) for u in usage)
    result = {
        "workflows": len(workflows), "completed": sum(w["status"] == "completed" for w in workflows),
        "outcomes": dict(Counter(w["status"] for w in workflows)),
        "mean_capped_completion_ms": mean(w["capped_completion_ms"] for w in workflows) if workflows else None,
        "post_tool_first_output_p90_ms": percentile90(timings) if timings and all(t is not None for t in timings) else None,
        "post_tool_timing_coverage": f"{sum(t is not None for t in timings)}/{len(post_tool)}",
        "output_limit_hits": sum(c.get("finish_reason") == "length" for c in calls),
        "recorded_message_mismatches": sum(c.get("recorded_message_match") is False for c in calls),
        "recorded_message_unknown": sum(c.get("recorded_message_match") is None for c in calls),
        "failed_final_notifications": sum(w.get("session_final", {}).get("status") != "sent" for w in workflows),
        "client_requests": len(calls), "session_final_requests": len(workflows),
        "client_input_tokens": total(u.get("prompt_tokens") for u in usage) if complete_usage else None,
        "client_output_tokens": total(u.get("completion_tokens") for u in usage) if complete_usage else None,
        "epoch_elapsed_s": epoch["elapsed_s"],
    }
    metrics = epoch.get("metrics", {})
    valid = metrics.get("valid_boundaries") is True
    for phase in ("prefill", "decode"):
        value = metrics.get("phase_totals", {}).get(phase, {}).get("seconds", {})
        result[f"server_{phase}_seconds"] = value.get("value") if valid and value.get("status") == "ok" else None
    for name in ("sampled_kv_fraction_seconds", "sampled_kv_max_fraction"):
        result[name] = metrics.get(name) if valid else None
    for name in ("generation_tokens", "prompt_tokens", "requests_finished"):
        value = metrics.get("counter_deltas", {}).get(name, {})
        result[f"server_{name}"] = value.get("value") if valid and value.get("status") == "ok" else None
    return result


def differences(rows, plan, metrics):
    by_run = {r["run_id"]: r for r in rows}
    if len(by_run) != len(rows):
        raise ValueError("Duplicate run ID")
    result = []
    for metric in metrics:
        deltas = []
        per_task = {}
        missing = []
        for task, repeat in sorted({(p.get("task", "replay"), p["repeat"]) for p in plan}):
            pair = {p["condition"]: by_run.get(p["run_id"]) for p in plan
                    if p["repeat"] == repeat and p.get("task", "replay") == task}
            a, b = pair.get("off"), pair.get("on")
            if a is None or b is None or a.get(metric) is None or b.get(metric) is None:
                missing.append({"task": task, "repeat": repeat})
            else:
                deltas.append(b[metric] - a[metric])
                per_task.setdefault(task, []).append(b[metric] - a[metric])
        result.append({"metric": metric, "pairs": len(deltas), "missing_pairs": missing,
                       "mean_on_minus_off": mean(mean(v) for v in per_task.values()) if per_task else None,
                       "per_task_mean": {k: mean(v) for k, v in per_task.items()},
                       "weighting": "equal weight per task with observed pairs",
                       "ci95": None, "interval_status": "cache_carryover_not_controlled"})
    return result


def save_report(output, report):
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    lines = ["# Dynamo baseline record", "", report["description"], "",
             f"Study state: {report['state']}. Hint application: unverified.", "",
             "| Condition | Recorded | Completed |", "| --- | ---: | ---: |"]
    for condition in ("off", "on"):
        rows = [r for r in report["rows"] if r["condition"] == condition]
        lines.append(f"| {condition} | {len(rows)} | {sum(r.get('completed', 0) for r in rows)} |")
    lines += ["", "Paired values are on minus off. Tasks with observed pairs receive equal weight. "
              "Missing pairs remain listed in `report.json`.", "",
              "| Measure | Pairs | Mean difference |", "| --- | ---: | ---: |"]
    for comparison in report["comparisons"]:
        value = comparison["mean_on_minus_off"]
        rendered = "unknown" if value is None else f"{value:.3f}"
        lines.append(f"| {comparison['metric']} | {comparison['pairs']} | {rendered} |")
    lines += ["", "The server cache carries between epochs or trials. These differences have no confidence interval. "
              "Client timing includes network and parsing. Server counters cover all traffic, including preparation "
              "and cleanup. A model response completing says nothing about coding-task correctness in replay.", ""]
    (output / "report.md").write_text("\n".join(lines))
    return report


def report_replay(output):
    output = Path(output)
    manifest = json.loads((output / "manifest.json").read_text())
    rows = []
    for record in manifest["epochs"]:
        row = {**record, "completed": 0}
        if record["status"] == "finished":
            epoch = checked_json(output / record["directory"] / "epoch.json", record.get("sha256"))
            row.update(epoch_values(epoch))
        rows.append(row)
    return save_report(output, {
        "study_kind": "dynamo_replay", "environment": manifest["environment"],
        "state": manifest["state"], "hint_application": "unverified",
        "description": "Fixed-request replay. Recorded counts are epochs; completed counts are workflows. "
                       + ("This record uses a mock transport and contains no model or GPU measurements."
                          if manifest["environment"] == "mock" else "Server behavior still needs trace verification."),
        "rows": rows, "comparisons": differences(rows, manifest["plan"], (
            "mean_capped_completion_ms", "post_tool_first_output_p90_ms", "client_input_tokens",
            "client_output_tokens", "output_limit_hits", "server_prefill_seconds", "server_decode_seconds",
            "server_generation_tokens", "server_prompt_tokens", "server_requests_finished",
            "sampled_kv_fraction_seconds")),
    })


def report_pilot(output):
    output = Path(output)
    manifest = json.loads((output / "manifest.json").read_text())
    rows = []
    for record in manifest["runs"]:
        row = {**record, "completed": int(record.get("reward") == 1 and not record.get("exception_type")),
               "trace_status": "missing", "reward": record.get("reward")}
        if record.get("trace"):
            path = output / record["trace"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != record.get("trace_sha256"):
                raise ValueError("Pilot trace checksum mismatch")
            metrics = analyze_trace(path)
            row.update(metrics, trace_status="present")
            if not metrics["telemetry_complete"]:
                row.update({key: None for key in ("model_latency_ms", "input_tokens", "model_calls", "tool_calls")})
        rows.append(row)
    return save_report(output, {
        "study_kind": "dynamo_feasibility", "state": manifest["state"],
        "description": "Harbor pilot. Recorded counts are trials; completed counts are verifier passes.",
        "rows": rows, "comparisons": differences(rows, manifest["plan"], (
            "reward", "input_tokens", "model_latency_ms", "model_calls", "tool_calls", "output_limit_hit")),
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    manifest = json.loads((args.directory / "manifest.json").read_text())
    if manifest["study_kind"] == "dynamo_replay":
        report_replay(args.directory)
    elif manifest["study_kind"] == "dynamo_feasibility":
        report_pilot(args.directory)
    else:
        parser.error("Expected a Dynamo study")


if __name__ == "__main__":
    main()
