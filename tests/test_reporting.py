import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from reporting import analyze_trace, average, load_rows
from run_experiments import export_run


class ReportingTests(unittest.TestCase):
    def test_missing_usage_does_not_become_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            path.write_text(json.dumps({"type": "inference", "input_tokens": 12,
                "cached_input_tokens": None, "output_tokens": 3, "latency_ms": 1}) + "\n")
            metrics = analyze_trace(path)
            self.assertEqual(metrics["input_tokens"], 12)
            self.assertIsNone(metrics["fresh_tokens"])
            self.assertIsNone(metrics["cached_tokens"])

    def test_error_attempt_remains_in_denominator(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "manifest.json").write_text(json.dumps({"runs": [
                {"run_id": "a", "cap_chars": 2000, "reward": 1},
                {"run_id": "b", "cap_chars": 2000, "reward": None,
                 "exception_type": "TimeoutError"},
            ]}))
            rows = load_rows(path)
            self.assertEqual(len(rows), 2)
            self.assertIsNone(average(rows, "reward"))
            self.assertIsNone(average(rows, "input_tokens"))

    def test_modified_evidence_fails_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "trace.jsonl").write_text("changed")
            (path / "manifest.json").write_text(json.dumps({"runs": [{
                "run_id": "a", "trace": "trace.jsonl",
                "trace_sha256": hashlib.sha256(b"original").hexdigest(),
            }]}))
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                load_rows(path)

    def test_missing_trial_is_exported_as_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = export_run(Path(tmp) / "missing", Path(tmp), "run", "hello", 2000, 1)
            self.assertIsNone(row["reward"])
            self.assertEqual(row["exception_type"], "MissingOrAmbiguousTrialResult")

    def test_historical_claims_recompute_from_evidence(self):
        root = Path(__file__).resolve().parents[1]
        for suite, expected_inputs in (("ab", (41645, 16596)),
                                       ("real_ab", (409810, 158767))):
            rows = load_rows(root / "experiments" / suite)
            self.assertEqual(len(rows), 6)
            self.assertTrue(all(r["reward"] == 1 and r["exception_type"] is None for r in rows))
            for cap, expected in zip((20000, 2000), expected_inputs):
                self.assertEqual(sum(r["input_tokens"] for r in rows if r["cap_chars"] == cap), expected)


if __name__ == "__main__":
    unittest.main()
