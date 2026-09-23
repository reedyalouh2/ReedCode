from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from reporting import paired_estimate, paired_results, pooled_results, summarize
from run_experiments import build_plan


def make_rows(plan, deltas):
    rows = []
    for item in plan:
        delta = 0 if item["condition"] == "head_20k" else deltas[item["task"]]
        rows.append({**item, "task_checksum": item["task"], "exception_type": None,
                     "telemetry_complete": True, "reward": 1,
                     "input_tokens": 1000 + delta, "fresh_tokens": 500 + delta,
                     "model_latency_ms": 100 + delta, "model_calls": 100 + delta,
                     "tool_calls": 100 + delta})
    return rows


def comparison(results, metric="model_calls", task=None):
    return next(r for r in results if r["baseline"] == "head_20k"
                and r["candidate"] == "head_2k" and r["metric"] == metric
                and (task is None or r["task"] == task))


class PooledReportingTests(unittest.TestCase):
    def test_equal_task_weights_with_unequal_pair_coverage(self):
        plan = build_plan(["few_pairs", "many_pairs"])
        rows = make_rows(plan, {"few_pairs": -10, "many_pairs": -90})
        rows = [r for r in rows if not (r["task"] == "few_pairs"
                                       and r["condition"] == "head_2k" and r["repeat"] > 1)]
        results = paired_results(rows, plan)
        for metric in ("model_calls", "tool_calls"):
            pooled = comparison(pooled_results(results), metric)
            self.assertEqual(pooled["mean_delta"], -50)
            self.assertNotEqual(pooled["mean_delta"], (-10 - 90 * 5) / 6)
            self.assertEqual(pooled["pairs"], 6)
            self.assertEqual(pooled["planned_pairs"], 10)
            self.assertEqual(pooled["included_task_count"], 2)
            self.assertEqual(pooled["complete_task_count"], 1)
            self.assertEqual(pooled["coverage"], "partial")
            self.assertEqual(pooled["scope"], "observed_subset")
            self.assertEqual(pooled["excluded_pairs"]["missing_run"], 4)
            self.assertIsNone(pooled["ci95"])

    def test_bootstrap_resamples_task_means(self):
        deltas = dict(zip("abcde", [-20, -10, 0, 10, 20]))
        plan = build_plan(list(deltas))
        per_task = paired_results(make_rows(plan, deltas), plan)
        pooled = comparison(pooled_results(per_task))
        expected = paired_estimate(list(deltas.values()))
        self.assertEqual(pooled["mean_delta"], expected["mean_delta"])
        self.assertEqual(pooled["ci95"], expected["ci95"])
        self.assertEqual(pooled["ci95_status"], "estimated")
        self.assertEqual(pooled["bootstrap_unit"], "task")
        self.assertEqual(pooled["task_weighting"], "equal")
        self.assertEqual(pooled["scope"], "complete_plan")
        self.assertEqual(pooled_results(per_task), pooled_results(list(reversed(per_task)))[::-1])

    def test_one_synthetic_task_has_no_across_task_interval(self):
        plan = build_plan(["synthetic"])
        per_task = paired_results(make_rows(plan, {"synthetic": -2}), plan)
        task = comparison(per_task)
        self.assertEqual(task["ci95"], [-2, -2])
        self.assertEqual(task["ci95_status"], "degenerate")
        pooled = comparison(pooled_results(per_task))
        self.assertEqual(pooled["mean_delta"], -2)
        self.assertEqual(pooled["included_task_count"], 1)
        self.assertIsNone(pooled["ci95"])
        self.assertEqual(pooled["ci95_status"], "insufficient_tasks")

    def test_identical_rewards_are_marked_degenerate(self):
        plan = build_plan(list("abcde"))
        rows = make_rows(plan, dict.fromkeys("abcde", -2))
        pooled = comparison(pooled_results(paired_results(rows, plan)), "reward")
        self.assertEqual(pooled["mean_delta"], 0)
        self.assertEqual(pooled["ci95"], [0, 0])
        self.assertEqual(pooled["ci95_status"], "degenerate")

    def test_exclusions_account_for_each_pair_and_keep_observed_failures(self):
        plan = build_plan(["task"], repeats=8)
        rows = make_rows(plan, {"task": -2})
        rows = [r for r in rows if not (r["condition"] == "head_2k" and r["repeat"] == 1)]
        candidates = {r["repeat"]: r for r in rows if r["condition"] == "head_2k"}
        candidates[2]["exception_type"] = "TimeoutError"
        candidates[3]["telemetry_complete"] = False
        candidates[4]["model_calls"] = None
        candidates[5]["task_checksum"] = None
        candidates[6]["task_checksum"] = "changed"
        candidates[7]["reward"] = 0
        per_task = paired_results(rows, plan)
        calls = comparison(per_task)
        self.assertEqual(calls["pairs"], 2)
        self.assertTrue(all(count == 1 for count in calls["excluded_pairs"].values()))
        self.assertEqual(calls["pairs"] + sum(calls["excluded_pairs"].values()), calls["planned_pairs"])
        self.assertEqual(calls["mean_delta"], -2)
        reward = comparison(per_task, "reward")
        self.assertEqual(reward["pairs"], 4)
        self.assertEqual(reward["mean_delta"], -0.25)

    def test_missing_tasks_do_not_look_like_complete_results(self):
        plan = build_plan(["observed", "missing"])
        rows = make_rows([p for p in plan if p["task"] == "observed"], {"observed": -2})
        pooled = comparison(pooled_results(paired_results(rows, plan)))
        self.assertEqual(pooled["included_tasks"], ["observed"])
        self.assertEqual(pooled["excluded_tasks"], ["missing"])
        self.assertEqual(pooled["planned_task_count"], 2)
        self.assertEqual(pooled["coverage"], "partial")
        self.assertEqual(pooled["scope"], "observed_subset")
        self.assertEqual(pooled["mean_delta"], -2)
        empty = comparison(pooled_results(paired_results([], plan)))
        self.assertIsNone(empty["mean_delta"])
        self.assertIsNone(empty["ci95"])
        self.assertEqual(empty["coverage"], "none")
        self.assertEqual(empty["excluded_tasks"], ["missing", "observed"])

    def test_json_and_cli_distinguish_plans_from_pooled_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manifest.json").write_text(json.dumps({"schema_version": 2,
                "state": "planned", "runs": [], "plan": build_plan(["a", "b"])}))
            with redirect_stdout(io.StringIO()) as output:
                summarize(root)
            report = json.loads((root / "paired_report.json").read_text())
            self.assertEqual(report["attempted"], 0)
            self.assertTrue(all(r["mean_delta"] is None for r in report["pooled_comparisons"]))
            self.assertIn("tasks=0/2", output.getvalue())
            self.assertIn("coverage=none", output.getvalue())
            self.assertIn("recovery calls", report["limitations"])


if __name__ == "__main__":
    unittest.main()
