import importlib.util
import json
import os
from copy import deepcopy
from pathlib import Path
import tarfile
import unittest

REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / "experiments/dynamo-prefix"
spec = importlib.util.spec_from_file_location("dynamo_renderer_audit", ROOT / "audit.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
TOKENIZER = Path(os.environ.get("REEDCODE_QWEN_TOKENIZER", "/tmp/reedcode-prefix-tokenizer/tokenizer.json"))
HAS_XXHASH = importlib.util.find_spec("xxhash") is not None


@unittest.skipUnless(HAS_XXHASH, "Install experiments/dynamo-prefix/requirements.txt")
class DynamoTraceHashTests(unittest.TestCase):
    def test_matches_published_dynamo_rust_test_vectors(self):
        self.assertEqual(audit.dynamo_input_sequence_hashes(list(range(1, 13)), 4),
                         [14643705804678351452, 4945711292740353085, 12583592247330656132])

    def test_partial_block_is_checked(self):
        ids = [1, 2, 3, 4, 5]
        record = {"input_tokens": 5, "replay": {"input_length": 5, "trace_block_size": 4,
                                                   "input_sequence_hashes": audit.dynamo_input_sequence_hashes(ids, 4)}}
        result = audit.validate_server_prompt(ids, record)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["compared_hashes"], 2)
        result = audit.validate_server_prompt([1, 2, 3, 4, 6], record)
        self.assertEqual(result["status"], "mismatch")
        self.assertEqual(result["first_mismatch_block"], 1)
        self.assertTrue(result["input_length_matches"])

    def test_incomplete_hash_record_cannot_pass(self):
        record = {"input_tokens": 5, "replay": {"input_length": 5, "trace_block_size": 4,
                                                   "input_sequence_hashes": [14643705804678351452]}}
        result = audit.validate_server_prompt([1, 2, 3, 4, 5], record)
        self.assertEqual(result["status"], "unverifiable")

    def test_invalid_token_ids_and_block_size_are_rejected(self):
        for ids, size in (([-1], 4), ([2**32], 4), ([True], 4), ([1], 0), ([1], True)):
            with self.assertRaises(ValueError):
                audit.dynamo_input_sequence_hashes(ids, size)

    def test_request_token_count_disagreement_cannot_pass(self):
        ids = [1, 2, 3, 4]
        record = {"input_tokens": 5, "replay": {"input_length": 4, "trace_block_size": 4,
                                                   "input_sequence_hashes": audit.dynamo_input_sequence_hashes(ids, 4)}}
        result = audit.validate_server_prompt(ids, record)
        self.assertEqual(result["status"], "mismatch")
        self.assertTrue(result["hashes_match"])


class MissingTraceTests(unittest.TestCase):
    def test_missing_or_wrong_event_stays_unverifiable(self):
        for record in (None, {}, {"event": None}, {"request": None},
                       {"event_type": "request_payload", "request": {}}):
            self.assertEqual(audit.validate_server_prompt([1], record)["status"], "unverifiable")


@unittest.skipUnless(TOKENIZER.exists() and HAS_XXHASH,
                     "Download the pinned tokenizer and install prefix requirements")
class CapturedRendererParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.renderer = audit.QwenRenderer(TOKENIZER)
        cls.archive = tarfile.open(REPO / "experiments/dynamo-20260928/raw-records.tar.gz", "r:gz")
        cls.addClassCleanup(cls.archive.close)
        events = [json.loads(line)["event"] for line in cls.archive.extractfile("server/frontend-trace.jsonl")]
        cls.payloads = {row["payload"]["request_id"]: row["payload"] for row in events
                        if row["event_type"] == "request_payload"}
        cls.ends = {row["request"]["request_id"]: row for row in events if row["event_type"] == "request_end"}

    def test_every_saved_server_input_matches_including_partial_blocks(self):
        self.assertEqual(len(self.payloads), 314)
        hashes = 0
        for request_id, row in self.payloads.items():
            with self.subTest(request_id=request_id):
                request = row["request"]
                ids = self.renderer.tokens(self.renderer.render(request, request["messages"], generation=True))
                self.assertEqual(len(ids), row["response"]["usage"]["prompt_tokens"])
                result = audit.validate_server_prompt(ids, self.ends[request_id])
                self.assertEqual(result["status"], "verified")
                hashes += result["compared_hashes"]
        self.assertEqual(hashes, 46759)

    def test_every_harbor_request_matches_its_own_server_record(self):
        count = 0
        for name in self.archive.getnames():
            if not name.startswith("pilot/noisy-bugfix_") or not name.endswith(".jsonl"):
                continue
            rows = [json.loads(line) for line in self.archive.extractfile(name)]
            requests = {row["turn"]: row["request"] for row in rows if row["type"] == "request"}
            for row in rows:
                if row["type"] != "inference":
                    continue
                request_id = row["completion_id"].removeprefix("chatcmpl-")
                with self.subTest(trial=name, turn=row["turn"]):
                    request = requests[row["turn"]]
                    ids = self.renderer.tokens(self.renderer.render(request, request["messages"], generation=True))
                    self.assertEqual(len(ids), row["input_tokens"])
                    self.assertEqual(audit.validate_server_prompt(ids, self.ends[request_id])["status"], "verified")
                    count += 1
        self.assertEqual(count, 52)

    def test_changing_json_argument_spacing_is_visible_to_qwen(self):
        rows = json.loads((ROOT / "fixtures/tool-on.json").read_text())
        request = rows[1]["request"]
        original = self.renderer.tokens(self.renderer.render(request, request["messages"], generation=True))
        compact = deepcopy(request)
        call = compact["messages"][2]["tool_calls"][0]
        call["function"]["arguments"] = '{"value":"ready"}'
        changed = self.renderer.tokens(self.renderer.render(compact, compact["messages"], generation=True))
        self.assertNotEqual(original, changed)

    def test_changing_tool_schema_key_order_is_visible_to_qwen(self):
        rows = json.loads((ROOT / "fixtures/tool-on.json").read_text())
        request = rows[0]["request"]
        original = self.renderer.tokens(self.renderer.render(request, request["messages"], generation=True))
        sorted_request = json.loads(json.dumps(request, sort_keys=True))
        changed = self.renderer.tokens(self.renderer.render(sorted_request, sorted_request["messages"], generation=True))
        self.assertNotEqual(original, changed)

    def test_null_assistant_content_and_omitted_content_match(self):
        rows = json.loads((ROOT / "fixtures/tool-on.json").read_text())
        request = rows[1]["request"]
        original = self.renderer.tokens(self.renderer.render(request, request["messages"], generation=True))
        with_null = deepcopy(request)
        with_null["messages"][2]["content"] = None
        changed = self.renderer.tokens(self.renderer.render(with_null, with_null["messages"], generation=True))
        self.assertEqual(original, changed)


if __name__ == "__main__":
    unittest.main()
