import contextlib
from copy import deepcopy
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import httpx


REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / "experiments/dynamo-upstream/001-speculative-prefill"
ARCHIVE = REPO / "experiments/dynamo-20260928/raw-records.tar.gz"
TOKENIZER = Path(os.environ.get("REEDCODE_QWEN_TOKENIZER", "/tmp/reedcode-prefix-tokenizer/tokenizer.json"))
HAS_XXHASH = importlib.util.find_spec("xxhash") is not None


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reproduce = load_module("dynamo_stock_reproduce", "reproduce.py")
probe = load_module("dynamo_stock_probe", "stock_probe.py")


def archived_tool_rows():
    with tarfile.open(ARCHIVE) as archive:
        return json.load(archive.extractfile("prefix-check/tool-on.json"))


def stream_lines(response, *, finish=None):
    choice = response["choices"][0]
    message = choice["message"]
    chunks = [{"id": response["id"], "choices": [{"index": 0, "delta": {"role": "assistant"}}]}]
    if message.get("tool_calls"):
        call = message["tool_calls"][0]
        function = call["function"]
        for i, (name, args) in enumerate(((function["name"][:7], function["arguments"][:3]),
                                         (function["name"][7:], function["arguments"][3:12]),
                                         (None, function["arguments"][12:]))):
            part = {"index": 0, "function": {"arguments": args}}
            if i == 0:
                part.update(id=call["id"], type="function")
            if name is not None:
                part["function"]["name"] = name
            chunks.append({"choices": [{"index": 0, "delta": {"tool_calls": [part]}}]})
    else:
        chunks.append({"choices": [{"index": 0, "delta": {"content": message.get("content", "")}}]})
    chunks.extend([{"choices": [{"index": 0, "delta": {},
                                 "finish_reason": choice["finish_reason"] if finish is None else finish}]},
                   {"choices": [], "usage": response["usage"]}])
    return [": keepalive", ""] + ["data: " + json.dumps(chunk) for chunk in chunks] + ["data: [DONE]"]


@unittest.skipUnless(HAS_XXHASH, "Install experiments/dynamo-prefix/requirements.txt")
class RoutingFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.ids = list(range(1, 13))
        self.record = {"block_size": 4, "isl_tokens": 12, "num_blocks": 3,
                       "local_hashes": json.dumps(reproduce.local_hashes(self.ids, 4))}

    def test_local_router_hashes_are_distinct_from_rolling_trace_hashes(self):
        local = reproduce.local_hashes(self.ids, 4)
        rolling = reproduce.audit.dynamo_input_sequence_hashes(self.ids, 4)
        self.assertEqual(rolling, [14643705804678351452, 4945711292740353085, 12583592247330656132])
        self.assertEqual(local[0], rolling[0])
        self.assertNotEqual(local[1:], rolling[1:])
        self.assertEqual(reproduce.check_routing(self.ids, self.record),
                         {"verified_full_blocks": 3, "unhashed_trailing_tokens": 0})
        with self.assertRaisesRegex(ValueError, "recorded routing"):
            reproduce.check_routing(self.ids, dict(self.record, local_hashes=json.dumps(rolling)))

    def test_changed_full_block_length_or_block_count_cannot_pass(self):
        changed = self.ids.copy()
        changed[5] += 1
        for ids, record in ((changed, self.record), (self.ids[:-1], self.record),
                            (self.ids, dict(self.record, num_blocks=2))):
            with self.subTest(ids=ids, record=record), self.assertRaises(ValueError):
                reproduce.check_routing(ids, record)

    def test_router_evidence_leaves_partial_block_unchecked(self):
        ids = self.ids + [13, 14]
        record = dict(self.record, isl_tokens=len(ids))
        result = reproduce.check_routing(ids, record)
        self.assertEqual(result["unhashed_trailing_tokens"], 2)
        self.assertEqual(reproduce.check_routing(self.ids + [90, 91], record), result)
        trace = {"input_tokens": len(ids), "replay": {
            "input_length": len(ids), "trace_block_size": 4,
            "input_sequence_hashes": reproduce.audit.dynamo_input_sequence_hashes(ids, 4)}}
        self.assertEqual(reproduce.audit.validate_server_prompt(ids, trace)["status"], "verified")
        self.assertEqual(reproduce.audit.validate_server_prompt(self.ids + [90, 91], trace)["status"], "mismatch")

    def test_invalid_block_size_cannot_make_hash_check_vacuous(self):
        for size in (0, -1, True, 1.5):
            with self.subTest(size=size), self.assertRaises(ValueError):
                reproduce.local_hashes(self.ids, size)


@unittest.skipUnless(TOKENIZER.exists() and HAS_XXHASH,
                     "Download the pinned tokenizer and install prefix requirements")
class ArchivedStockReproductionTests(unittest.TestCase):
    def test_reproduction_verifies_stock_wire_requests_and_runtime_hashes(self):
        verifier = reproduce.audit.validate_server_prompt
        with patch.object(reproduce.audit, "validate_server_prompt", wraps=verifier) as observed:
            result = reproduce.reproduce(TOKENIZER)
        self.assertEqual(observed.call_count, 4)
        self.assertEqual({call.args[1]["request"]["request_id"] for call in observed.call_args_list}, {
            "d5360dd8-7b9b-4916-a2f2-33279b20c025", "d8c6a27c-cc15-4152-b635-9f82c1750f0a",
            "59fd1e70-5c6a-48a3-9642-a2305cb37fee", "d5a3f0ff-0cdf-407a-ae67-207807687e79"})
        self.assertEqual(result["archive_sha256"], "4a4650d97c06173e817e21657a651daa7d9d1be20441db92869fb42a584a1cb1")
        self.assertEqual(result["extracted_evidence"]["verified_files"], 30)
        self.assertEqual(result["new_gpu_requests"], 0)
        for case, expected in zip(result["cases"], (("text", 64, 78, 56, 48, 0), ("tool", 72, 244, 40, 192, 8))):
            name, prepared, actual, divergence, cached, unhashed = expected
            with self.subTest(case=name):
                self.assertEqual(case["case"], name)
                self.assertEqual(case["source"], "stock_nvext_agent_hints_speculative_prefill")
                self.assertEqual((case["speculative_tokens"], case["followup_tokens"]), (prepared, actual))
                self.assertEqual(case["exact_divergence_from_cpu_reconstruction"], divergence)
                self.assertEqual(case["cached_followup_off"], cached)
                self.assertEqual(case["cached_followup_on"], cached)
                self.assertEqual(case["prepared_input_matches_routing_hashes"]["unhashed_trailing_tokens"], unhashed)
                self.assertEqual(case["additional_matching_full_block_tokens"], 0)

    def test_reproduction_cannot_ignore_failed_server_fingerprint_check(self):
        with patch.object(reproduce.audit, "validate_server_prompt", return_value={"status": "mismatch"}):
            with self.assertRaisesRegex(ValueError, "server input fingerprints"):
                reproduce.reproduce(TOKENIZER)

    def test_captured_wire_hints_are_present_on_both_historical_requests(self):
        records = reproduce.survey.read_records(ARCHIVE)
        events = [json.loads(line)["event"] for line in records["server/frontend-trace.jsonl"].splitlines()]
        payloads = {e["payload"]["request_id"]: e["payload"] for e in events if e["event_type"] == "request_payload"}
        for name in ("text", "tool"):
            for capture in json.loads(records[f"prefix-check/{name}-on.json"]):
                request_id = capture["response"]["id"].removeprefix("chatcmpl-")
                wire = payloads[request_id]
                with self.subTest(request_id=request_id):
                    self.assertEqual(wire["endpoint"], "openai.chat_completion")
                    self.assertTrue(wire["payload_complete"])
                    self.assertIs(wire["request"]["nvext"]["agent_hints"]["speculative_prefill"], True)
                    self.assertEqual(wire["request"]["messages"], capture["request"]["messages"])


class StockStreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = archived_tool_rows()

    def test_fragmented_tool_call_recreates_archived_followup(self):
        response = probe.parse_response(stream_lines(self.rows[0]["response"]))
        initial = deepcopy(self.rows[0]["request"])
        initial["nvext"] = {"agent_hints": {"speculative_prefill": True}}
        following = probe.followup(initial, response, "tool")
        self.assertEqual(following["messages"], self.rows[1]["request"]["messages"])
        self.assertEqual(following["tools"], initial["tools"])
        self.assertEqual(response["message"]["tool_calls"][0]["function"]["arguments"], '{"value": "ready"}')
        self.assertIs(initial["nvext"]["agent_hints"]["speculative_prefill"], True)
        self.assertIs(following["nvext"]["agent_hints"]["speculative_prefill"], False)
        self.assertEqual(len(initial["messages"]), 2)

    def test_cutoff_missing_usage_or_missing_finish_is_rejected(self):
        lines = stream_lines(self.rows[0]["response"])
        cases = [stream_lines(self.rows[0]["response"], finish="length"),
                 [line for line in lines if '"usage"' not in line],
                 [line for line in lines if '"finish_reason"' not in line],
                 [line for line in lines if '"id": "chatcmpl-' not in line]]
        for lines in cases:
            with self.subTest(lines=lines), self.assertRaisesRegex(ValueError, "Incomplete"):
                probe.parse_response(lines)

    def test_conflicting_tool_ids_are_rejected(self):
        lines = stream_lines(self.rows[0]["response"])
        lines.insert(-1, 'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"different"}]}}]}')
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            probe.parse_response(lines)

    def test_unexpected_or_incomplete_tool_arguments_prevent_followup(self):
        request = probe.request_pair_start("tool", True, "test")
        parsed = probe.parse_response(stream_lines(self.rows[0]["response"]))
        for args in ('{"value":', '{"value":"something else"}', '{"value":"ready","extra":true}'):
            response = deepcopy(parsed)
            response["message"]["tool_calls"][0]["function"]["arguments"] = args
            with self.subTest(args=args), self.assertRaises(ValueError):
                probe.followup(request, response, "tool")

    def test_text_followup_preserves_reasoning_and_turn_order(self):
        request = probe.request_pair_start("text", True, "test")
        lines = ['data: {"id":"chatcmpl-test","choices":[{"index":0,"delta":{"reasoning_content":"Plan "}}]}',
                 'data: {"choices":[{"index":0,"delta":{"reasoning_content":"first.","content":"ready"},"finish_reason":"stop"}]}',
                 'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5}}']
        response = probe.parse_response(lines)
        following = probe.followup(request, response, "text")
        self.assertEqual(following["messages"][-2], {"role": "assistant", "reasoning_content": "Plan first.", "content": "ready"})
        self.assertEqual(following["messages"][-1], {"role": "user", "content": "Now reply with exactly done."})
        self.assertIs(following["nvext"]["agent_hints"]["speculative_prefill"], False)


class StockProbeHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.output = self.directory / "output"
        self.deployment = self.directory / "deployment.json"
        self.deployment.write_text(json.dumps({"dynamo_version": "1.5.0", "container_image": "test-image",
                                               "model_revision": reproduce.audit.REVISION, "launch_command": "test-command"}))
        self.argv = ["stock_probe.py", "--base-url", "http://test.invalid/v1", "--case", "tool",
                     "--condition", "on", "--output", str(self.output), "--deployment", str(self.deployment)]
        self.rows = archived_tool_rows()

    def run_mocked(self, responses):
        received = []

        def handler(request):
            received.append(request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text="\n\n".join(responses[len(received) - 1]) + "\n\n")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch.object(probe.httpx, "Client", return_value=client), patch("sys.argv", self.argv), \
                patch.object(probe.time, "sleep") as sleep, contextlib.redirect_stdout(io.StringIO()):
            probe.main()
        return received, sleep

    def test_sends_only_two_chat_requests_with_stock_hint_and_keeps_raw_responses(self):
        key = self.directory / "api-key"
        key.write_text("mock-test-key\n")
        self.argv.extend(["--api-key-file", str(key)])
        streams = [stream_lines(row["response"]) for row in self.rows]
        received, sleep = self.run_mocked(streams)
        self.assertEqual(len(received), 2)
        sleep.assert_called_once_with(2)
        bodies = [json.loads(request.content) for request in received]
        for turn, (request, body) in enumerate(zip(received, bodies)):
            self.assertEqual(str(request.url), "http://test.invalid/v1/chat/completions")
            self.assertEqual(request.method, "POST")
            self.assertNotIn("prompt", body)
            self.assertTrue(body["stream"])
            self.assertTrue(body["stream_options"]["include_usage"])
            self.assertEqual(body["tools"], self.rows[0]["request"]["tools"])
            self.assertEqual(request.headers["Authorization"], "Bearer mock-test-key")
            self.assertEqual(json.loads((self.output / f"request-{turn}.json").read_text()), body)
            self.assertIn("data: [DONE]", (self.output / f"response-{turn}.sse").read_text())
            self.assertTrue((self.output / f"parsed-{turn}.json").exists())
        self.assertIs(bodies[0]["nvext"]["agent_hints"]["speculative_prefill"], True)
        self.assertIs(bodies[1]["nvext"]["agent_hints"]["speculative_prefill"], False)
        self.assertEqual(bodies[1]["messages"][:2], bodies[0]["messages"])
        self.assertEqual(bodies[1]["messages"][2:], self.rows[1]["request"]["messages"][2:])
        self.assertEqual(received[0].headers["X-Dynamo-Session-ID"], received[1].headers["X-Dynamo-Session-ID"])
        manifest = (self.output / "manifest.json").read_text()
        self.assertNotIn("mock-test-key", manifest)
        self.assertFalse(json.loads(manifest)["deployment_independently_verified_by_client"])

    def test_incomplete_response_is_saved_and_never_sent_back_to_server(self):
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            self.run_mocked([stream_lines(self.rows[0]["response"], finish="length")])
        self.assertIn('"finish_reason": "length"', (self.output / "response-0.sse").read_text())
        self.assertFalse((self.output / "request-1.json").exists())

    def test_non_cold_start_prevents_followup(self):
        response = deepcopy(self.rows[0]["response"])
        response["usage"]["prompt_tokens_details"]["cached_tokens"] = 16
        with self.assertRaisesRegex(ValueError, "cold prefix"):
            self.run_mocked([stream_lines(response)])
        self.assertFalse((self.output / "request-1.json").exists())

    def test_invalid_url_is_rejected_before_creating_artifacts(self):
        for url in ("file:///tmp/server", "http://user:pass@test.invalid/v1", "https://test.invalid/v1?key=secret", "https://test.invalid/v1#fragment"):
            argv = self.argv.copy()
            argv[2] = url
            with self.subTest(url=url), patch("sys.argv", argv), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                probe.main()
            self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
