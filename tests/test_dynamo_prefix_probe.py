import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import httpx

ROOT = Path(__file__).resolve().parents[1] / "experiments/dynamo-prefix"
spec = importlib.util.spec_from_file_location("dynamo_prefix_probe", ROOT / "probe_gpu.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
TOKENIZER = Path(os.environ.get("REEDCODE_QWEN_TOKENIZER", "/tmp/reedcode-prefix-tokenizer/tokenizer.json"))


class UsageTests(unittest.TestCase):
    def test_missing_cache_usage_is_unknown(self):
        with self.assertRaises(ValueError):
            probe.cached_tokens({"usage": {"prompt_tokens": 200}})

    def test_zero_cache_hit_is_valid(self):
        self.assertEqual(probe.cached_tokens({"usage": {"prompt_tokens_details": {"cached_tokens": 0}}}), 0)

    def test_null_details_stay_unknown(self):
        with self.assertRaises(ValueError):
            probe.cached_tokens({"usage": {"prompt_tokens_details": None}})

    def test_base_url_rejects_credentials_and_query_strings(self):
        for value in ("http://user:secret@localhost/v1", "http://localhost/v1?key=secret",
                      "file:///tmp/server", "http://localhost/v1#section", "http://localhost:bad/v1"):
            with self.assertRaises(ValueError):
                probe.validated_base_url(value)
        self.assertEqual(probe.validated_base_url("http://127.0.0.1:18000/v1"), "http://127.0.0.1:18000/v1/")

    def test_cli_refuses_to_overwrite_evidence_before_loading_tokenizer(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "existing.json"
            output.write_text("original evidence")
            result = subprocess.run([sys.executable, str(ROOT / "probe_gpu.py"),
                                     "--tokenizer", str(Path(directory) / "missing.json"),
                                     "--server-identity", "test-deployment", "--output", str(output)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("Output exists", result.stderr)
            self.assertEqual(output.read_text(), "original evidence")


@unittest.skipUnless(TOKENIZER.exists(), "Download the pinned tokenizer; set REEDCODE_QWEN_TOKENIZER")
class ProbeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.renderer = probe.audit.QwenRenderer(TOKENIZER)

    async def test_probe_records_every_preparation_request_and_cache_result(self):
        prefixes = set()
        bodies = []

        def respond(request):
            body = json.loads(request.content)
            bodies.append(body)
            ids = tuple(body["prompt"])
            cached = max((len(prefix) for prefix in prefixes if ids[:len(prefix)] == prefix), default=0)
            for end in range(16, len(ids) + 1, 16):
                prefixes.add(ids[:end])
            return httpx.Response(200, json={"usage": {"prompt_tokens": len(ids), "completion_tokens": 1,
                                                       "prompt_tokens_details": {"cached_tokens": cached}}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="http://local/v1/") as client:
            report = await probe.run_probe(client, self.renderer)
        self.assertEqual(len(bodies), 10)
        self.assertTrue(all(body["max_tokens"] == 1 for body in bodies))
        self.assertTrue(report["all_conditions_match_expected_cache_reuse"])
        self.assertFalse(report["server_side_patch_installed_by_probe"])
        for row in report["cases"]:
            if row["condition"] == "explicit_preparation":
                self.assertEqual([call["stage"] for call in row["calls"]], ["initial", "prepare", "followup"])
                self.assertEqual(row["total_reported_completion_tokens"], 3)
            else:
                self.assertEqual([call["stage"] for call in row["calls"]], ["initial", "followup"])
                self.assertEqual(row["total_reported_completion_tokens"], 2)

    async def test_cold_prefix_check_fails_and_retains_response(self):
        def respond(request):
            ids = json.loads(request.content)["prompt"]
            return httpx.Response(200, json={"usage": {"prompt_tokens": len(ids), "completion_tokens": 1,
                                                       "prompt_tokens_details": {"cached_tokens": 16}}})

        report = {}
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="http://local/v1/") as client:
            with self.assertRaisesRegex(ValueError, "cold-prefix"):
                await probe.run_probe(client, self.renderer, report=report)
        self.assertEqual(len(report["cases"]), 1)
        self.assertEqual(report["cases"][0]["calls"][0]["cached_tokens"], 16)

    async def test_server_tokenization_mismatch_fails(self):
        def respond(request):
            return httpx.Response(200, json={"usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                                       "prompt_tokens_details": {"cached_tokens": 0}}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="http://local/v1/") as client:
            with self.assertRaisesRegex(ValueError, "submitted token IDs"):
                await probe.run_probe(client, self.renderer)

    async def test_cache_mismatch_is_reported_without_dropping_the_trial(self):
        def respond(request):
            body = json.loads(request.content)
            return httpx.Response(200, json={"usage": {"prompt_tokens": len(body["prompt"]), "completion_tokens": 1,
                                                       "prompt_tokens_details": {"cached_tokens": 0}}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="http://local/v1/") as client:
            report = await probe.run_probe(client, self.renderer, case_names=("tool",))
        self.assertFalse(report["all_conditions_match_expected_cache_reuse"])
        self.assertEqual(len(report["cases"]), 2)
        self.assertEqual(sum(len(row["calls"]) for row in report["cases"]), 5)

    def test_nonce_changes_the_first_cache_block(self):
        first = probe.probe_case(self.renderer, "tool", "3b60cf5ad7304bdc977206dbe23e6c6b", 16)
        second = probe.probe_case(self.renderer, "tool", "9e08660253744ab7a8721d4f3a074879", 16)
        self.assertNotEqual(first["initial_prompt_token_ids"][:16], second["initial_prompt_token_ids"][:16])
        self.assertLess(first["ordinary_matching_tokens"], len(first["initial_prompt_token_ids"]))
        self.assertEqual(first["prepared_token_ids"], first["actual_followup_token_ids"][:len(first["prepared_token_ids"])])

    def test_long_argument_probe_is_explicitly_synthetic(self):
        nonce = "3b60cf5ad7304bdc977206dbe23e6c6b"
        original = probe.probe_case(self.renderer, "tool", nonce, 16)
        long = probe.probe_case(self.renderer, "tool_long", nonce, 16)
        self.assertEqual(original["initial_prompt_token_ids"], long["initial_prompt_token_ids"])
        self.assertGreaterEqual(long["expected_additional_full_block_tokens"], 512)
        self.assertTrue(long["provenance"]["synthetic_assistant"])
        self.assertFalse(long["provenance"]["model_generated_long_arguments"])
        self.assertFalse(long["provenance"]["tool_was_executed"])
        self.assertEqual(long["prepared_token_ids"], long["actual_followup_token_ids"][:len(long["prepared_token_ids"])])


if __name__ == "__main__":
    unittest.main()
