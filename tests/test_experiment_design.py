from collections import Counter
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import run_experiments as runner
from reporting import analyze_trace, paired_estimate, paired_results, summarize
from run_experiments import build_plan, REAL_TASKS, CONDITIONS


class ExperimentDesignTests(unittest.TestCase):
    def test_plan_is_reproducible_complete_and_interleaved(self):
        plan = build_plan(REAL_TASKS)
        self.assertEqual(len(plan), 150)
        self.assertEqual(plan, build_plan(REAL_TASKS))
        self.assertNotEqual(plan, build_plan(REAL_TASKS, seed=1))
        self.assertEqual(len({row["run_id"] for row in plan}), 150)
        counts = Counter((p["task"], p["condition"]) for p in plan)
        self.assertTrue(all(n == 5 for n in counts.values()))
        for offset in range(0, len(plan), 3):
            block = plan[offset:offset + 3]
            self.assertEqual(len({(p["task"], p["repeat"]) for p in block}), 1)
            self.assertEqual({p["condition"] for p in block}, set(CONDITIONS))
        for task in REAL_TASKS:
            orders = {tuple(p["condition"] for p in plan if p["task"] == task and p["repeat"] == r)
                      for r in range(1, 6)}
            self.assertEqual(len(orders), 5)

    def test_pairing_keeps_failures_and_reports_missing_pairs(self):
        plan = build_plan(["task"])
        rows = [{**p, "input_tokens": 100 if p["condition"] == "head_20k" else 60,
                 "reward": 0, "task_checksum": "same", "telemetry_complete": True,
                 "exception_type": None} for p in plan]
        result = next(r for r in paired_results(rows, plan) if r["baseline"] == "head_20k"
                      and r["candidate"] == "head_2k" and r["metric"] == "input_tokens")
        self.assertEqual(result["mean_delta"], -40)
        self.assertEqual(result["pairs"], 5)
        self.assertEqual(result["ci95"], [-40, -40])
        damaged = next(r for r in rows if r["condition"] == "head_2k")
        for field, value in (("exception_type", "Timeout"), ("telemetry_complete", False),
                             ("input_tokens", None), ("task_checksum", "changed")):
            before = damaged[field]
            damaged[field] = value
            result = next(r for r in paired_results(rows, plan) if r["baseline"] == "head_20k"
                          and r["candidate"] == "head_2k" and r["metric"] == "input_tokens")
            self.assertEqual(result["pairs"], 4)
            self.assertEqual(result["planned_pairs"], 5)
            self.assertIsNone(result["ci95"])
            damaged[field] = before

    def test_bootstrap_and_duplicate_block_checks(self):
        estimate = paired_estimate([-3, -1, 0, 2, 7])
        self.assertEqual(estimate["mean_delta"], 1)
        self.assertLess(estimate["ci95"][0], 1)
        self.assertGreater(estimate["ci95"][1], 1)
        self.assertIsNone(paired_estimate([1])["ci95"])
        plan = build_plan(["task"], repeats=1)
        with self.assertRaisesRegex(ValueError, "Duplicate condition"):
            paired_results([plan[0], plan[0]], plan)

    def test_old_traces_have_unknown_truncation_and_plan_is_not_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trace = root / "trace.jsonl"
            trace.write_text(json.dumps({"type": "tool", "output_bytes": 100}) + "\n")
            self.assertIsNone(analyze_trace(trace)["truncated_calls"])
            (root / "manifest.json").write_text(json.dumps({"schema_version": 2,
                "state": "planned", "runs": [], "plan": build_plan(["task"])}))
            with redirect_stdout(io.StringIO()) as out:
                summarize(root)
            self.assertIn("Attempted 0/15", out.getvalue())
            report = json.loads((root / "paired_report.json").read_text())
            self.assertTrue(all(r["pairs"] == 0 for r in report["comparisons"]))

    def test_runner_records_three_conditions_and_stops_on_bad_oracle(self):
        for oracle_reward in (0, 1):
            with self.subTest(oracle_reward=oracle_reward), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for name in ("reedcode_harbor_agent.py", "output_policy.py", "run_experiments.py",
                             "reporting.py", "model_backend.py", "server_metrics.py", "uv.lock"):
                    (root / name).write_text("test source")
                source = root / "evals/noisy-bugfix"
                source.mkdir(parents=True)
                (source / "task.toml").write_text('version = "1.0"\n')
                output = root / "output"
                model_conditions = []

                def fake_harbor(command, **kwargs):
                    oracle = command[command.index("--agent") + 1] == "oracle"
                    job = root / "jobs" / command[command.index("--job-name") + 1]
                    trial = job / "trial"
                    trial.mkdir(parents=True)
                    (trial / "result.json").write_text(json.dumps({
                        "task_checksum": "fixed-task", "verifier_result": {
                            "rewards": {"reward": oracle_reward if oracle else 0}},
                    }))
                    if not oracle:
                        env = kwargs["env"]
                        model_conditions.append((int(env["MAX_TOOL_OUTPUT"]), env["OUTPUT_POLICY"]))
                        events = [
                            {"type": "inference", "input_tokens": 10, "cached_input_tokens": 2,
                             "output_tokens": 1, "latency_ms": 5},
                            {"type": "task_summary", "stop_reason": "no_tool_calls"},
                        ]
                        (trial / "reedcode_trace.jsonl").write_text(
                            "\n".join(json.dumps(e) for e in events) + "\n")
                    return SimpleNamespace(returncode=0)

                argv = ["runner", "synthetic", "--repeats", "1", "--output", str(output)]
                with patch.object(runner, "ROOT", root), patch("sys.argv", argv), \
                     patch.dict(os.environ, {"OPENAI_API_KEY": "unused-test-placeholder"}), \
                     patch.object(runner.shutil, "which", return_value="harbor"), \
                     patch.object(runner.subprocess, "run", side_effect=fake_harbor), \
                     redirect_stdout(io.StringIO()):
                    status = runner.main()
                manifest = json.loads((output / "manifest.json").read_text())
                if oracle_reward:
                    self.assertEqual(status, 0)
                    self.assertEqual(set(model_conditions), set(CONDITIONS.values()))
                    self.assertEqual(len(manifest["runs"]), 3)
                    self.assertTrue(all(r["reward"] == 0 for r in manifest["runs"]))
                    self.assertEqual(manifest["state"], "finished")
                    self.assertTrue((output / "paired_report.json").exists())
                else:
                    self.assertEqual(status, 1)
                    self.assertEqual(model_conditions, [])
                    self.assertEqual(manifest["runs"], [])
                    self.assertEqual(manifest["state"], "preflight_failed")


if __name__ == "__main__":
    unittest.main()
