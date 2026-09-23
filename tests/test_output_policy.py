import os
from pathlib import Path
import unittest
from unittest.mock import patch

from output_policy import Settings, retain_output


class OutputPolicyTests(unittest.TestCase):
    def test_boundary_no_truncation(self):
        for policy in ("head", "head_tail"):
            for text in ("", "a", "abcde"):
                result, meta = retain_output(text, 5, policy)
                self.assertEqual(result, text)
                self.assertFalse(meta["truncated"])

    def test_head_tail_retains_budget_without_overlap(self):
        text = "0123456789é"
        for limit in (1, 2, 3, 10):
            result, meta = retain_output(text, limit, "head_tail")
            self.assertTrue(meta["truncated"])
            self.assertEqual(meta["retained_output_chars"], limit)
            self.assertEqual(meta["original_output_bytes"], 12)
            self.assertTrue(result.startswith(text[:(limit + 1) // 2]))
            if limit > 1:
                self.assertTrue(result.endswith(text[-(limit // 2):]))

    def test_settings_are_read_per_instance_and_validate(self):
        with patch.dict(os.environ, {"MAX_TOOL_OUTPUT": "7", "OUTPUT_POLICY": "head_tail"}):
            first = Settings.from_env()
        with patch.dict(os.environ, {"MAX_TOOL_OUTPUT": "11", "OUTPUT_POLICY": "head"}):
            second = Settings.from_env()
        self.assertEqual(first.max_tool_output, 7)
        self.assertEqual(second.max_tool_output, 11)
        for kwargs in ({"max_turns": 0}, {"max_tool_output": -1}, {"tool_timeout": 0},
                       {"output_policy": "unknown"}, {"model_api": "unknown"},
                       {"max_output_tokens": 0}, {"metrics_sample_interval": float("nan")},
                       {"metrics_sample_interval": 0}):
            with self.assertRaises(ValueError):
                Settings(**kwargs)

    def test_inference_settings_do_not_store_credentials(self):
        with patch.dict(os.environ, {"MODEL_API": "chat", "MAX_OUTPUT_TOKENS": "4096",
                                     "OPENAI_API_KEY": "private-test-value",
                                     "VLLM_METRICS_URL": "http://localhost:8000/metrics"}):
            settings = Settings.from_env()
        self.assertEqual(settings.model_api, "chat")
        self.assertEqual(settings.max_output_tokens, 4096)
        self.assertEqual(settings.server_metrics_url, "http://localhost:8000/metrics")
        self.assertNotIn("private-test-value", repr(settings))

    def test_verifier_uses_the_same_pristine_suite(self):
        task = Path(__file__).resolve().parents[1] / "evals" / "noisy-bugfix"
        self.assertEqual((task / "tests/test_pricing.py").read_bytes(),
                         (task / "environment/data/test_pricing.py").read_bytes())


if __name__ == "__main__":
    unittest.main()
