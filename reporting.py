"""Read experiment manifests and summarize their traces."""

import argparse
import hashlib
import json
import random
from pathlib import Path
from statistics import mean


def known_sum(values):
    values = list(values)
    return None if not values or any(v is None for v in values) else sum(values)


def analyze_trace(path):
    events = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    inference = [e for e in events if e.get("type") == "inference"]
    tools = [e for e in events if e.get("type") == "tool"]
    metrics = {
        "input_tokens": known_sum(e.get("input_tokens") for e in inference),
        "cached_tokens": known_sum(e.get("cached_input_tokens") for e in inference),
        "output_tokens": known_sum(e.get("output_tokens") for e in inference),
        "model_calls": len(inference),
        "tool_calls": len(tools),
        "model_latency_ms": known_sum(e.get("latency_ms") for e in inference),
        "tool_latency_ms": known_sum(e.get("duration_ms") for e in tools) if tools else 0,
        "tool_output_bytes": known_sum(e.get("output_bytes") for e in tools) if tools else 0,
        "truncated_calls": known_sum(e.get("truncated") for e in tools) if tools else 0,
        "original_output_chars": known_sum(e.get("original_output_chars") for e in tools) if tools else 0,
        "retained_output_chars": known_sum(e.get("retained_output_chars") for e in tools) if tools else 0,
        "telemetry_complete": any(e.get("type") == "task_summary" and
            e.get("stop_reason") in ("no_tool_calls", "max_turns") for e in events),
    }
    metrics["fresh_tokens"] = (
        metrics["input_tokens"] - metrics["cached_tokens"]
        if metrics["input_tokens"] is not None and metrics["cached_tokens"] is not None
        else None
    )
    return metrics


def load_rows(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    rows = []
    seen = set()
    for run in manifest["runs"]:
        if run["run_id"] in seen:
            raise ValueError(f"Duplicate run: {run['run_id']}")
        seen.add(run["run_id"])
        metrics = {key: None for key in (
            "input_tokens", "cached_tokens", "fresh_tokens", "output_tokens",
            "model_calls", "tool_calls", "model_latency_ms", "tool_latency_ms", "tool_output_bytes",
            "truncated_calls", "original_output_chars", "retained_output_chars", "telemetry_complete",
        )}
        if run.get("trace"):
            path = directory / run["trace"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != run["trace_sha256"]:
                raise ValueError(f"Trace checksum mismatch: {path}")
            metrics = analyze_trace(path)
        rows.append({**run, **metrics})
    if not rows:
        raise ValueError("Manifest has no runs")
    return rows


def average(rows, key):
    values = [r.get(key) for r in rows]
    return None if not values or any(v is None for v in values) else mean(values)


def display(value):
    return "unknown" if value is None else f"{value:,.2f}"


def summarize(directory):
    manifest = json.loads((Path(directory) / "manifest.json").read_text())
    if manifest.get("schema_version", 1) >= 2:
        return summarize_paired(directory, manifest)
    rows = load_rows(directory)
    metrics = (
        "reward", "input_tokens", "cached_tokens", "fresh_tokens", "output_tokens",
        "model_calls", "tool_calls", "model_latency_ms", "tool_latency_ms",
        "tool_output_bytes", "job_runtime_s",
    )
    groups = {cap: [r for r in rows if r["cap_chars"] == cap] for cap in (20000, 2000)}
    for cap, group in groups.items():
        print(f"\nCAP {cap}: {len(group)} attempted trials")
        for r in group:
            print(f"  {r['run_id']}: reward={display(r.get('reward'))} "
                  f"exception={r.get('exception_type') or 'none'} "
                  f"input={display(r['input_tokens'])} fresh={display(r['fresh_tokens'])}")
        print(f"  Passed: {sum(r.get('reward') == 1 for r in group)}/{len(group)}; "
              f"missing rewards: {sum(r.get('reward') is None for r in group)}; "
              f"exceptions: {sum(r.get('exception_type') is not None for r in group)}")
        for key in metrics:
            print(f"  {key:23} {display(average(group, key))}")
    # Compare only matching task and repetition counts.
    task_counts = lambda group: sorted(r['task'] for r in group)
    if not groups[20000] or task_counts(groups[20000]) != task_counts(groups[2000]):
        print("\nRelative changes unavailable: unmatched task/repetition sets.")
        return
    print("\n20K -> 2K (relative change in group means; all attempts included)")
    for key in metrics:
        a, b = (average(groups[cap], key) for cap in (20000, 2000))
        change = "unknown" if a in (None, 0) or b is None else f"{(b-a)/a*100:+.1f}%"
        print(f"  {key:23} {change}")

def paired_estimate(values, bootstrap_samples=10000, seed=0):
    """Percentile bootstrap of block-level differences; no interval below five pairs."""
    if not values:
        return {"pairs": 0, "mean_delta": None, "ci95": None}
    estimate = {"pairs": len(values), "mean_delta": mean(values), "ci95": None}
    if len(values) >= 5:
        rng = random.Random(seed)
        samples = sorted(mean(rng.choices(values, k=len(values))) for _ in range(bootstrap_samples))
        estimate["ci95"] = [samples[int(0.025 * bootstrap_samples)],
                            samples[int(0.975 * bootstrap_samples) - 1]]
    return estimate


def paired_results(rows, plan):
    index = {}
    for row in rows:
        key = (row["task"], row["repeat"], row["condition"])
        if key in index:
            raise ValueError(f"Duplicate condition in block: {key}")
        index[key] = row
    results = []
    comparisons = (("head_20k", "head_2k"), ("head_2k", "head_tail_2k"),
                   ("head_20k", "head_tail_2k"))
    for task in sorted({p["task"] for p in plan}):
        repeats = sorted({p["repeat"] for p in plan if p["task"] == task})
        for baseline, candidate in comparisons:
            for metric in ("reward", "input_tokens", "fresh_tokens", "model_latency_ms"):
                deltas = []
                for repeat in repeats:
                    a = index.get((task, repeat, baseline))
                    b = index.get((task, repeat, candidate))
                    if a is None or b is None:
                        continue
                    if a.get("exception_type") or b.get("exception_type"):
                        continue
                    # Aborted call totals understate cost; retain them in raw rows only.
                    if metric != "reward" and not (a.get("telemetry_complete") and b.get("telemetry_complete")):
                        continue
                    if a.get(metric) is None or b.get(metric) is None:
                        continue
                    if not a.get("task_checksum") or a["task_checksum"] != b.get("task_checksum"):
                        continue
                    deltas.append(b[metric] - a[metric])
                results.append({"task": task, "baseline": baseline, "candidate": candidate,
                                "metric": metric, "planned_pairs": len(repeats),
                                **paired_estimate(deltas)})
    return results


def summarize_paired(directory, manifest):
    rows = load_rows(directory) if manifest["runs"] else []
    plan = manifest["plan"]
    print(f"Attempted {len(rows)}/{len(plan)} scheduled trials; state={manifest['state']}")
    for task in sorted({p["task"] for p in plan}):
        for condition in ("head_20k", "head_2k", "head_tail_2k"):
            group = [r for r in rows if r["task"] == task and r["condition"] == condition]
            planned = sum(p["task"] == task and p["condition"] == condition for p in plan)
            passed = sum(r.get("reward") == 1 and not r.get("exception_type") for r in group)
            missing = sum(r.get("reward") is None for r in group)
            exceptions = sum(bool(r.get("exception_type")) for r in group)
            print(f"{task} {condition}: passed={passed}/{len(group)} attempts, planned={planned}, "
                  f"missing rewards={missing}, exceptions={exceptions}; "
                  f"truncated calls={display(known_sum(r.get('truncated_calls') for r in group))}")
    results = paired_results(rows, plan)
    print("\nPaired differences (candidate minus baseline), per task:")
    for row in results:
        interval = row["ci95"]
        ci = "unavailable (<5 pairs)" if interval is None else f"[{display(interval[0])}, {display(interval[1])}]"
        print(f"{row['task']} {row['candidate']} - {row['baseline']} {row['metric']}: "
              f"{display(row['mean_delta'])}; 95% interval {ci}; "
              f"pairs={row['pairs']}/{row['planned_pairs']}")
    report = {
        "method": "per-task mean paired differences; 10,000 block bootstrap resamples; percentile 95% intervals",
        "limitations": "Exploratory intervals, especially with five pairs. Missing/exception pairs are excluded and counted. "
                       "Pairing controls task and time block, not model randomness. No task-population inference.",
        "attempted": len(rows), "planned": len(plan), "comparisons": results,
    }
    (Path(directory) / "paired_report.json").write_text(json.dumps(report, indent=2) + "\n")
    print("\nExploratory intervals; inspect pair coverage and failures before interpreting savings.")


def main(default_directory):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", default=default_directory)
    args = parser.parse_args()
    summarize(args.directory)
