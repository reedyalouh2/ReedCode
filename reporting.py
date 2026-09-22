"""Read experiment manifests and summarize their traces."""

import argparse
import hashlib
import json
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


def main(default_directory):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", default=default_directory)
    args = parser.parse_args()
    summarize(args.directory)
