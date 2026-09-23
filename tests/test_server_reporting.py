from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from reporting import SERVER_METRICS, analyze_trace, load_rows, paired_results, pooled_results, summarize
from run_experiments import build_plan


def window(prefill=0.25, decode=0.5, cache=0.2):
    return {
        "status": "partial", "successful_scrapes": 3, "failed_scrapes": 0,
        "server_restarted": False, "attribution_warning": False,
        "counter_deltas": {"requests_finished": {"status": "ok", "value": 1}},
        "histograms": {
            "prefill": {"status": "ok", "count": 1, "sum_seconds": prefill},
            "decode": {"status": "ok", "count": 1, "sum_seconds": decode},
        },
        "gauges": {"kv_cache_usage_fraction": {
            "unit": "fraction", "during_sample_count": 1, "during_sampled_max": cache,
            "before": 0.95, "after": 0.99,
        }},
    }


def read_measurements(windows, extra_events=()):
    events = [{"type": "inference", "server_metrics": value} for value in windows]
    events.extend(extra_events)
    with tempfile.TemporaryDirectory() as tmp:
        trace = Path(tmp) / "trace.jsonl"
        trace.write_text("\n".join(json.dumps(event) for event in events) + "\n")
        return analyze_trace(trace)


class ServerReportingTests(unittest.TestCase):
    def test_sums_phase_times_but_takes_maximum_during_call_cache_sample(self):
        metrics = read_measurements([window(), window(prefill=0.5, decode=1, cache=0.6)])
        self.assertEqual(metrics["server_prefill_seconds"], 0.75)
        self.assertEqual(metrics["server_decode_seconds"], 1.5)
        self.assertEqual(metrics["server_kv_sampled_max_fraction"], 0.6)
        self.assertTrue(metrics["server_metrics_enabled"])

    def test_missing_or_disabled_call_prevents_partial_totals(self):
        for missing in (None, {}, {"status": "disabled"}, {"status": "unavailable"}):
            with self.subTest(missing=missing):
                metrics = read_measurements([window(), missing])
                self.assertTrue(all(metrics[key] is None for key in SERVER_METRICS))
        for windows in ([], [None], [{"status": "disabled"}]):
            metrics = read_measurements(windows)
            self.assertFalse(metrics["server_metrics_enabled"])
            self.assertTrue(all(metrics[key] is None for key in SERVER_METRICS))

    def test_restart_concurrency_or_failed_scrape_invalidates_all_server_aggregates(self):
        problems = []
        for key, value in (("server_restarted", True), ("attribution_warning", True),
                           ("failed_scrapes", 1), ("successful_scrapes", 1)):
            damaged = window()
            damaged[key] = value
            problems.append(damaged)
        for status in ("counter_reset", "series_changed", "server_restarted", "scrape_failed"):
            damaged = window()
            damaged["counter_deltas"]["requests_finished"] = {"status": status, "value": None}
            problems.append(damaged)
        damaged = window()
        damaged["counter_deltas"]["prompt_tokens"] = {"status": "counter_reset", "value": None}
        problems.append(damaged)
        for damaged in problems:
            with self.subTest(window=damaged):
                metrics = read_measurements([window(), damaged])
                self.assertTrue(all(metrics[key] is None for key in SERVER_METRICS))

    def test_each_phase_requires_one_valid_observation_per_call(self):
        for field, value in (("count", 0), ("count", 2), ("count", None), ("count", float("nan")),
                             ("status", "missing"), ("sum_seconds", None), ("sum_seconds", "0.25"),
                             ("sum_seconds", float("nan")), ("sum_seconds", float("inf")),
                             ("sum_seconds", -1)):
            damaged = window()
            damaged["histograms"]["prefill"][field] = value
            with self.subTest(field=field, value=value):
                metrics = read_measurements([window(), damaged])
                self.assertIsNone(metrics["server_prefill_seconds"])
                self.assertEqual(metrics["server_decode_seconds"], 1)
        zero = read_measurements([window(prefill=0, decode=0, cache=0)])
        self.assertTrue(all(zero[key] == 0 for key in SERVER_METRICS))

    def test_kv_requires_during_samples_each_call_and_preserves_fraction_units(self):
        for field, value in (("during_sample_count", 0), ("during_sample_count", None),
                             ("during_sampled_max", None), ("during_sampled_max", float("nan")),
                             ("during_sampled_max", float("inf")), ("during_sampled_max", "0.5"),
                             ("during_sampled_max", -0.5), ("during_sampled_max", 20),
                             ("unit", "percent")):
            damaged = window()
            damaged["gauges"]["kv_cache_usage_fraction"][field] = value
            with self.subTest(field=field, value=value):
                metrics = read_measurements([window(), damaged])
                self.assertIsNone(metrics["server_kv_sampled_max_fraction"])
                self.assertEqual(metrics["server_prefill_seconds"], 0.5)

    def test_failed_model_call_does_not_leave_a_partial_server_total(self):
        metrics = read_measurements([window()], [
            {"type": "failed_inference_metrics", "server_metrics": window()},
            {"type": "task_summary", "stop_reason": "error"},
        ])
        self.assertTrue(all(metrics[key] is None for key in SERVER_METRICS))
        self.assertFalse(metrics["telemetry_complete"])

    def test_missing_trace_keeps_unknown_values_and_manifest_enablement(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "manifest.json").write_text(json.dumps({"server_metrics_enabled": True,
                "runs": [{"run_id": "failed", "trace": None}]}))
            row = load_rows(directory)[0]
            self.assertTrue(row["server_metrics_enabled"])
            self.assertTrue(all(row[key] is None for key in SERVER_METRICS))

    def test_server_metrics_are_opt_in_for_paired_and_pooled_reports(self):
        plan = build_plan(["task"], repeats=1)
        hosted = [{**p, "telemetry_complete": True, "task_checksum": "fixed", "reward": 1}
                  for p in plan]
        self.assertFalse(any(r["metric"] in SERVER_METRICS for r in paired_results(hosted, plan)))
        rows = deepcopy(hosted)
        for row in rows:
            row.update(read_measurements([window(prefill=0.5 if row["condition"] == "head_20k" else 0.25)]))
            row["telemetry_complete"] = True
        for results in (paired_results(rows, plan), pooled_results(paired_results(rows, plan))):
            prefill = next(r for r in results if r["metric"] == "server_prefill_seconds"
                           and r["baseline"] == "head_20k" and r["candidate"] == "head_2k")
            self.assertEqual(prefill["mean_delta"], -0.25)
            self.assertEqual(prefill["pairs"], 1)
        unknown = paired_results(hosted, plan, server_metrics_enabled=True)
        self.assertTrue(any(r["metric"] in SERVER_METRICS for r in unknown))
        self.assertTrue(all(r["pairs"] == 0 for r in unknown if r["metric"] in SERVER_METRICS))

    def test_exported_report_contains_valid_server_measurements(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            plan = build_plan(["task"], repeats=1)
            runs = []
            for item in plan:
                trace = directory / f"{item['run_id']}.jsonl"
                events = [{"type": "inference", "server_metrics": window()},
                          {"type": "task_summary", "stop_reason": "no_tool_calls"}]
                trace.write_text("\n".join(json.dumps(event) for event in events) + "\n")
                runs.append({**item, "task_checksum": "fixed", "reward": 1,
                             "trace": trace.name, "trace_sha256": hashlib.sha256(trace.read_bytes()).hexdigest()})
            (directory / "manifest.json").write_text(json.dumps({"schema_version": 2,
                "server_metrics_enabled": True, "state": "finished", "runs": runs, "plan": plan}))
            with redirect_stdout(io.StringIO()):
                summarize(directory)
            report = json.loads((directory / "paired_report.json").read_text())
            self.assertEqual({row["metric"] for row in report["pooled_comparisons"]} & set(SERVER_METRICS),
                             set(SERVER_METRICS))


if __name__ == "__main__":
    unittest.main()
