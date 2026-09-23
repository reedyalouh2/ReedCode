"""Read experiment manifests and summarize their traces."""

import argparse
from collections import Counter
import hashlib
import json
import math
import random
from pathlib import Path
from statistics import mean


COMPARISONS = (("head_20k", "head_2k"), ("head_2k", "head_tail_2k"),
               ("head_20k", "head_tail_2k"))
PAIRED_METRICS = ("reward", "input_tokens", "fresh_tokens", "model_latency_ms",
                  "model_calls", "tool_calls", "output_limit_hit")
SERVER_METRICS = ("server_prefill_seconds", "server_decode_seconds", "server_kv_sampled_max_fraction")
EXCLUSION_REASONS = ("missing_run", "exception", "incomplete_telemetry", "missing_metric",
                     "missing_task_checksum", "task_checksum_mismatch")


def known_sum(values):
    values = list(values)
    return None if not values or any(v is None for v in values) else sum(values)


def finite_nonnegative(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def valid_server_window(window):
    if not isinstance(window, dict) or window.get("status") not in ("ok", "partial"):
        return False
    if window.get("attribution_warning") is not False or window.get("server_restarted") is not False:
        return False
    if window.get("failed_scrapes") != 0 or not finite_nonnegative(window.get("successful_scrapes")):
        return False
    if window["successful_scrapes"] < 2:
        return False
    counters, histograms = window.get("counter_deltas", {}), window.get("histograms", {})
    if not isinstance(counters, dict) or not isinstance(histograms, dict):
        return False
    for item in (*counters.values(), *histograms.values()):
        if not isinstance(item, dict) or item.get("status") in ("counter_reset", "server_restarted", "series_changed"):
            return False
    finished = counters.get("requests_finished", {})
    # A valid completion delta also establishes that both boundary scrapes succeeded.
    return (finished.get("status") == "ok" and finite_nonnegative(finished.get("value"))
            and finished["value"] == 1)


def server_measurements(inference):
    result = dict.fromkeys(SERVER_METRICS)
    windows = [event.get("server_metrics") for event in inference]
    if not windows or not all(valid_server_window(window) for window in windows):
        return result
    for phase in ("prefill", "decode"):
        values = []
        for window in windows:
            histogram = window.get("histograms", {}).get(phase, {})
            valid = (histogram.get("status") == "ok" and finite_nonnegative(histogram.get("count"))
                     and histogram["count"] == 1 and finite_nonnegative(histogram.get("sum_seconds")))
            values.append(histogram["sum_seconds"] if valid else None)
        total = known_sum(values)
        result[f"server_{phase}_seconds"] = total if finite_nonnegative(total) else None
    cache_values = []
    for window in windows:
        gauge = window.get("gauges", {}).get("kv_cache_usage_fraction", {})
        value, samples = gauge.get("during_sampled_max"), gauge.get("during_sample_count")
        valid = (gauge.get("unit") == "fraction" and finite_nonnegative(samples) and samples >= 1
                 and finite_nonnegative(value) and value <= 1)
        cache_values.append(value if valid else None)
    if all(value is not None for value in cache_values):
        result["server_kv_sampled_max_fraction"] = max(cache_values)
    return result


def analyze_trace(path):
    events = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    inference = [e for e in events if e.get("type") == "inference"]
    tools = [e for e in events if e.get("type") == "tool"]
    summaries = [e for e in events if e.get("type") == "task_summary"]
    summary = summaries[-1] if summaries else {}
    stop_reason = summary.get("stop_reason")
    if "output_limit_hit" in summary:
        value = summary["output_limit_hit"]
        output_limit_hit = value if isinstance(value, bool) else None
    elif stop_reason in ("no_tool_calls", "max_turns", "max_output_tokens"):
        output_limit_hit = stop_reason == "max_output_tokens"
    else:
        output_limit_hit = None
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
        "telemetry_complete": stop_reason in ("no_tool_calls", "max_turns", "max_output_tokens"),
        "output_limit_hit": output_limit_hit,
        "server_metrics_enabled": any(e.get("server_metrics_enabled") is True or
            (isinstance(e.get("server_metrics"), dict) and e["server_metrics"].get("status")
             not in (None, "disabled")) for e in events),
    }
    metrics.update(server_measurements(inference))
    if any(e.get("type") == "failed_inference_metrics" for e in events):
        metrics.update(dict.fromkeys(SERVER_METRICS))
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
            "output_limit_hit", "server_metrics_enabled", *SERVER_METRICS,
        )}
        if run.get("trace"):
            path = directory / run["trace"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != run["trace_sha256"]:
                raise ValueError(f"Trace checksum mismatch: {path}")
            metrics = analyze_trace(path)
        if manifest.get("server_metrics_enabled"):
            metrics["server_metrics_enabled"] = True
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
        return {"pairs": 0, "mean_delta": None, "ci95": None,
                "ci95_status": "insufficient_pairs"}
    estimate = {"pairs": len(values), "mean_delta": mean(values), "ci95": None,
                "ci95_status": "insufficient_pairs"}
    if len(values) >= 5:
        rng = random.Random(seed)
        samples = sorted(mean(rng.choices(values, k=len(values))) for _ in range(bootstrap_samples))
        estimate["ci95"] = [samples[int(0.025 * bootstrap_samples)],
                            samples[int(0.975 * bootstrap_samples) - 1]]
        estimate["ci95_status"] = ("degenerate" if estimate["ci95"][0] == estimate["ci95"][1]
                                   else "estimated")
    return estimate


def pair_exclusion(a, b, metric):
    """Return one reason per excluded pair, in the order checked here."""
    if a is None or b is None:
        return "missing_run"
    if a.get("exception_type") or b.get("exception_type"):
        return "exception"
    # Aborted call totals understate cost; retain them in raw rows only.
    if metric != "reward" and not (a.get("telemetry_complete") and b.get("telemetry_complete")):
        return "incomplete_telemetry"
    if a.get(metric) is None or b.get(metric) is None:
        return "missing_metric"
    if not a.get("task_checksum") or not b.get("task_checksum"):
        return "missing_task_checksum"
    if a["task_checksum"] != b["task_checksum"]:
        return "task_checksum_mismatch"
    return None


def paired_results(rows, plan, server_metrics_enabled=False):
    index = {}
    for row in rows:
        key = (row["task"], row["repeat"], row["condition"])
        if key in index:
            raise ValueError(f"Duplicate condition in block: {key}")
        index[key] = row
    results = []
    include_server = server_metrics_enabled or any(row.get("server_metrics_enabled") or
        any(row.get(metric) is not None for metric in SERVER_METRICS) for row in rows)
    metrics = PAIRED_METRICS + SERVER_METRICS if include_server else PAIRED_METRICS
    for task in sorted({p["task"] for p in plan}):
        repeats = sorted({p["repeat"] for p in plan if p["task"] == task})
        for baseline, candidate in COMPARISONS:
            for metric in metrics:
                deltas = []
                exclusions = Counter({reason: 0 for reason in EXCLUSION_REASONS})
                for repeat in repeats:
                    a = index.get((task, repeat, baseline))
                    b = index.get((task, repeat, candidate))
                    reason = pair_exclusion(a, b, metric)
                    if reason:
                        exclusions[reason] += 1
                        continue
                    deltas.append(b[metric] - a[metric])
                results.append({"task": task, "baseline": baseline, "candidate": candidate,
                                "metric": metric, "planned_pairs": len(repeats),
                                "excluded_pairs": dict(exclusions),
                                **paired_estimate(deltas)})
    return results


def pooled_results(per_task_results, bootstrap_samples=10000, seed=0):
    """Average task means equally, then resample tasks rather than individual trials."""
    groups = {}
    for result in per_task_results:
        key = (result["baseline"], result["candidate"], result["metric"])
        groups.setdefault(key, []).append(result)
    results = []
    for (baseline, candidate, metric), group in groups.items():
        group = sorted(group, key=lambda r: r["task"])
        if len({r["task"] for r in group}) != len(group):
            raise ValueError("Duplicate task in pooled comparison")
        included = [r for r in group if r["pairs"] > 0]
        estimate = paired_estimate([r["mean_delta"] for r in included], bootstrap_samples, seed)
        complete = all(r["pairs"] == r["planned_pairs"] for r in group)
        results.append({
            "baseline": baseline, "candidate": candidate, "metric": metric,
            "mean_delta": estimate["mean_delta"], "ci95": estimate["ci95"],
            "ci95_status": ("insufficient_tasks" if estimate["ci95_status"] == "insufficient_pairs"
                            else estimate["ci95_status"]),
            "bootstrap_unit": "task", "task_weighting": "equal",
            "included_task_count": len(included), "planned_task_count": len(group),
            "included_tasks": [r["task"] for r in included],
            "excluded_tasks": [r["task"] for r in group if r["pairs"] == 0],
            "complete_task_count": sum(r["pairs"] == r["planned_pairs"] for r in group),
            "pairs": sum(r["pairs"] for r in group),
            "planned_pairs": sum(r["planned_pairs"] for r in group),
            "coverage": "none" if not included else "complete" if complete else "partial",
            "scope": "complete_plan" if complete else "observed_subset",
            "excluded_pairs": {reason: sum(r["excluded_pairs"][reason] for r in group)
                               for reason in EXCLUSION_REASONS},
        })
    return results


def interval_text(result, unit):
    interval = result["ci95"]
    if interval is None:
        return f"unavailable (<5 {unit})"
    text = f"[{display(interval[0])}, {display(interval[1])}]"
    if result["ci95_status"] == "degenerate":
        text += " (degenerate; not evidence of certainty)"
    return text


def condition_summaries(rows, plan):
    summaries = []
    for task in sorted({p["task"] for p in plan}):
        for condition in ("head_20k", "head_2k", "head_tail_2k"):
            group = [r for r in rows if r["task"] == task and r["condition"] == condition]
            observed = sum(isinstance(r.get("output_limit_hit"), bool) for r in group)
            hits = sum(r.get("output_limit_hit") is True for r in group)
            summaries.append({
                "task": task, "condition": condition,
                "attempted": len(group),
                "planned": sum(p["task"] == task and p["condition"] == condition for p in plan),
                "passed": sum(r.get("reward") == 1 and not r.get("exception_type") for r in group),
                "missing_rewards": sum(r.get("reward") is None for r in group),
                "exceptions": sum(bool(r.get("exception_type")) for r in group),
                "output_limit_hits": hits,
                "output_limit_observed": observed,
                "output_limit_unknown": len(group) - observed,
                "output_limit_hit_rate": hits / observed if observed else None,
                "truncated_calls": known_sum(r.get("truncated_calls") for r in group),
            })
    return summaries


def summarize_paired(directory, manifest):
    rows = load_rows(directory) if manifest["runs"] else []
    plan = manifest["plan"]
    print(f"Attempted {len(rows)}/{len(plan)} scheduled trials; state={manifest['state']}")
    conditions = condition_summaries(rows, plan)
    for row in conditions:
        rate = row["output_limit_hit_rate"]
        rate_text = "unknown" if rate is None else f"{rate:.1%}"
        print(f"{row['task']} {row['condition']}: passed={row['passed']}/{row['attempted']} attempts, "
              f"planned={row['planned']}, missing rewards={row['missing_rewards']}, "
              f"exceptions={row['exceptions']}; output limit hits={row['output_limit_hits']}/"
              f"{row['output_limit_observed']} known ({rate_text}), unknown={row['output_limit_unknown']}; "
              f"truncated calls={display(row['truncated_calls'])}")
    results = paired_results(rows, plan, manifest.get("server_metrics_enabled", False))
    print("\nPaired differences (candidate minus baseline), per task:")
    for row in results:
        print(f"{row['task']} {row['candidate']} - {row['baseline']} {row['metric']}: "
              f"{display(row['mean_delta'])}; 95% interval {interval_text(row, 'pairs')}; "
              f"pairs={row['pairs']}/{row['planned_pairs']}")
        if sum(row["excluded_pairs"].values()):
            print("  Excluded pairs: " + ", ".join(f"{k}={v}" for k, v in row["excluded_pairs"].items() if v))
    pooled = pooled_results(results)
    print("\nPooled differences (equal weight per observed task; candidate minus baseline):")
    for row in pooled:
        print(f"{row['candidate']} - {row['baseline']} {row['metric']}: "
              f"{display(row['mean_delta'])}; 95% task-bootstrap interval {interval_text(row, 'tasks')}; "
              f"tasks={row['included_task_count']}/{row['planned_task_count']}; "
              f"pairs={row['pairs']}/{row['planned_pairs']}; coverage={row['coverage']}")
        if row["excluded_tasks"]:
            print("  No eligible pairs for: " + ", ".join(row["excluded_tasks"]))
    report = {
        "schema_version": 2,
        "method": "Per-task mean paired differences, bootstrapped over repetition blocks. "
                  "Pooled estimate: equal-weight mean of observed task means, bootstrapped over tasks. "
                  "10,000 resamples, seed 0, percentile 95% intervals; at least five resampling units required.",
        "limitations": "Exploratory intervals, especially with five pairs or few tasks. Degenerate intervals do not "
                       "establish equivalence or certainty. Missing/exception pairs are excluded and counted; partial "
                       "coverage estimates describe the observed subset, not the complete plan. Pairing controls task "
                       "and time block, not model randomness. Task bootstrap holds observed task means fixed; it is "
                       "not a separate estimate of within-task run uncertainty. Selected tasks are not a random "
                       "sample of Terminal-Bench. Model and tool calls are total workload counts, not identified "
                       "recovery calls; inspect traces to attribute a call to hidden output. Output-token limits "
                       "are normal budget stops: verifier rewards and completed-call telemetry remain in the "
                       "analysis. Limit-hit rates use trials with a known marker; unknown outcomes are counted "
                       "separately. Paired limit-hit differences are differences in 0/1 indicators. When enabled, server "
                       "phase totals require valid samples for every model call. They are server wall times, not "
                       "GPU kernel timings. KV occupancy is the maximum during-call sample across calls and engines, "
                       "not a capacity-weighted fraction or a continuous peak, and excludes tool and idle time.",
        "attempted": len(rows), "planned": len(plan), "comparisons": results,
        "condition_summaries": conditions,
        "pooled_comparisons": pooled,
    }
    (Path(directory) / "paired_report.json").write_text(json.dumps(report, indent=2) + "\n")
    print("\nExploratory intervals; inspect pair coverage and failures before interpreting savings.")


def main(default_directory):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", default=default_directory)
    args = parser.parse_args()
    summarize(args.directory)
