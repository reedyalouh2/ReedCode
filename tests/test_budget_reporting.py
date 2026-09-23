from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from reporting import analyze_trace, load_rows, paired_results, pooled_results, summarize
from run_experiments import build_plan


def write_trace(directory, name, summary, calls=2, tools=3):
    events = [{"type": "inference", "input_tokens": 10, "cached_input_tokens": 2,
               "output_tokens": 4, "latency_ms": 5} for _ in range(calls)]
    events += [{"type": "tool", "duration_ms": 1, "output_bytes": 10, "truncated": False,
                "original_output_chars": 10, "retained_output_chars": 10} for _ in range(tools)]
    if summary is not None:
        events.append({"type": "task_summary", **summary})
    path = directory / f"{name}.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    return path


def comparison(results, metric):
    return next(row for row in results if row["baseline"] == "head_20k"
                and row["candidate"] == "head_2k" and row["metric"] == metric)


class BudgetReportingTests(unittest.TestCase):
    def test_budget_failure_keeps_reward_usage_and_calls_in_paired_and_pooled_results(self):
        plan = build_plan(["task"], repeats=1)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            rows = []
            for item in plan:
                limited = item["condition"] == "head_2k"
                path = write_trace(directory, item["run_id"], {
                    "stop_reason": "max_output_tokens" if limited else "no_tool_calls",
                    "output_limit_hit": limited,
                }, calls=3 if limited else 2, tools=4 if limited else 3)
                rows.append({**item, "task_checksum": "same", "exception_type": None,
                             "reward": 0 if limited else 1, **analyze_trace(path)})
            results = paired_results(rows, plan)
            for result_set in (results, pooled_results(results)):
                for metric, expected in (("reward", -1), ("input_tokens", 10), ("fresh_tokens", 8),
                                         ("model_latency_ms", 5), ("model_calls", 1),
                                         ("tool_calls", 1), ("output_limit_hit", 1)):
                    with self.subTest(metric=metric):
                        result = comparison(result_set, metric)
                        self.assertEqual(result["pairs"], 1)
                        self.assertEqual(result["mean_delta"], expected)
                        self.assertEqual(sum(result["excluded_pairs"].values()), 0)

    def test_budget_stop_can_still_pass_verification_and_infrastructure_errors_stay_excluded(self):
        plan = build_plan(["task"], repeats=1)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            rows = []
            for item in plan:
                limited = item["condition"] == "head_2k"
                path = write_trace(directory, item["run_id"], {
                    "stop_reason": "max_output_tokens" if limited else "no_tool_calls",
                    "output_limit_hit": limited,
                })
                rows.append({**item, "task_checksum": "same", "reward": 1,
                             "exception_type": None, **analyze_trace(path)})
            result = comparison(paired_results(rows, plan), "reward")
            self.assertEqual(result["pairs"], 1)
            self.assertEqual(result["mean_delta"], 0)
            candidate = next(row for row in rows if row["condition"] == "head_2k")
            candidate["exception_type"] = "ConnectionError"
            for result in paired_results(rows, plan):
                if result["baseline"] == "head_20k" and result["candidate"] == "head_2k":
                    self.assertEqual(result["pairs"], 0)
                    self.assertEqual(result["excluded_pairs"]["exception"], 1)

    def test_missing_and_aborted_markers_stay_unknown(self):
        cases = (
            (None, None, False),
            ({"stop_reason": "error"}, None, False),
            ({"stop_reason": "cancelled_or_task_timeout", "output_limit_hit": None}, None, False),
            ({"stop_reason": "no_tool_calls"}, False, True),
            ({"stop_reason": "max_turns"}, False, True),
            ({"stop_reason": "max_output_tokens"}, True, True),
            ({"stop_reason": "no_tool_calls", "output_limit_hit": "false"}, None, True),
        )
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for number, (summary, marker, complete) in enumerate(cases):
                with self.subTest(summary=summary):
                    metrics = analyze_trace(write_trace(directory, str(number), summary))
                    self.assertIs(metrics["output_limit_hit"], marker)
                    self.assertIs(metrics["telemetry_complete"], complete)
            (directory / "manifest.json").write_text(json.dumps({
                "runs": [{"run_id": "missing_trace", "trace": None}]}))
            self.assertIsNone(load_rows(directory)[0]["output_limit_hit"])

    def test_limit_rates_show_known_denominator_unknowns_and_all_attempts(self):
        plan = build_plan(["task"], repeats=5)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            runs = []
            for item in plan:
                if item["condition"] != "head_2k" or item["repeat"] == 5:
                    continue
                repeat = item["repeat"]
                summary = ({"stop_reason": "max_output_tokens", "output_limit_hit": True}
                           if repeat <= 2 else {"stop_reason": "no_tool_calls"}
                           if repeat == 3 else {"stop_reason": "error"})
                path = write_trace(directory, item["run_id"], summary)
                runs.append({**item, "task_checksum": "same", "reward": (0, 1, 1, None)[repeat - 1],
                             "exception_type": "ConnectionError" if repeat == 4 else None,
                             "trace": path.name, "trace_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
            (directory / "manifest.json").write_text(json.dumps({"schema_version": 2,
                "state": "running", "runs": runs, "plan": plan}))
            with redirect_stdout(io.StringIO()) as output:
                summarize(directory)
            report = json.loads((directory / "paired_report.json").read_text())
            row = next(row for row in report["condition_summaries"] if row["condition"] == "head_2k")
            self.assertEqual((row["passed"], row["attempted"], row["planned"]), (2, 4, 5))
            self.assertEqual((row["output_limit_hits"], row["output_limit_observed"],
                              row["output_limit_unknown"]), (2, 3, 1))
            self.assertEqual(row["output_limit_hit_rate"], 2 / 3)
            self.assertEqual((row["missing_rewards"], row["exceptions"]), (1, 1))
            empty = next(row for row in report["condition_summaries"] if row["condition"] == "head_20k")
            self.assertIsNone(empty["output_limit_hit_rate"])
            self.assertIn("passed=2/4 attempts, planned=5", output.getvalue())
            self.assertIn("output limit hits=2/3 known (66.7%), unknown=1", output.getvalue())


if __name__ == "__main__":
    unittest.main()
