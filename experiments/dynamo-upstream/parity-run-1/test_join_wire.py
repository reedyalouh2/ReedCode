import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from join_wire import backend_usage, build_mapping
from report import build_report


class JoinTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.session = self.root / "codex"
        self.folder = self.session / "capture/0001"
        self.folder.mkdir(parents=True)
        self.raw = b'{"model":"fixture"}'
        self.response = ('data: ' + json.dumps({"type": "response.completed", "response": {
            "usage": {"input_tokens": 32, "output_tokens": 1,
                      "input_tokens_details": {"cached_tokens": 0}}}}) + '\n\n').encode()
        self.meta = {"path": "/v1/responses", "inference": True, "complete": True,
                     "response_status": 200, "started_monotonic_ns": 20, "ended_monotonic_ns": 25,
                     "observer_x_request_id": "observer-1", "original_x_request_ids": [],
                     "request_sha256": hashlib.sha256(self.raw).hexdigest(),
                     "response_sha256": hashlib.sha256(self.response).hexdigest()}
        (self.folder / "request.body").write_bytes(self.raw)
        (self.folder / "response.body").write_bytes(self.response)
        self.result = {"client": "codex", "session_id": "session", "stub_only": True,
                       "turns": [{"started_monotonic_ns": 10, "ended_monotonic_ns": 30}]}
        self.log = self.root / "frontend.log"
        self.log_row = {"message": "request received", "request_id": "internal-9",
                        "x_request_id": "observer-1"}
        self.log.write_text(json.dumps(self.log_row) + "\n")
        self.wire = {"requests": [{"request_id": "internal-9", "kind": "model", "complete": True,
                     "input_token_ids": list(range(32)), "http_link": {
                         "http_request_id": "observer-1", "runtime_request_id": "internal-9",
                         "frontend_log": str(self.log), "line": 1, "row": self.log_row},
                     "responses": [{"wrapper": {"data": {"data": {"completion_usage": {
                         "prompt_tokens": 32, "prompt_tokens_details": {"cached_tokens": 0}}}}}}]}],
                     "frontend_log_linkage": {"files": {str(self.log): hashlib.sha256(self.log.read_bytes()).hexdigest()}}}
        self.identity = self.root / "identity.json"
        self.identity.write_text('{"worker":"fixture","epoch":"one"}')

    def tearDown(self):
        self.temp.cleanup()

    def mapping(self, **kwargs):
        (self.folder / "metadata.json").write_text(json.dumps(self.meta))
        (self.session / "result.json").write_text(json.dumps(self.result))
        path = self.root / "wire.json"
        path.write_text(json.dumps(self.wire))
        return build_mapping(path, {"codex": self.session}, "worker", "epoch", self.identity,
                             self.root / "mapping.json", main_reviewed=True, **kwargs)

    def test_exact_bridge_and_backend_zero_produce_usable_report(self):
        mapping = self.mapping()
        entry = mapping["sessions"][0]["requests"][0]
        self.assertEqual(entry["backend"]["request_id"], "internal-9")
        self.assertEqual(entry["backend_usage"]["cached_tokens"], 0)
        self.assertTrue(build_report(mapping, self.root)["requests"][0]["usable"])

    def test_original_header_is_supported(self):
        self.meta.update(observer_x_request_id=None, original_x_request_ids=["observer-1"])
        self.assertEqual(self.mapping()["join"]["unlinked_capture_ids"], [])

    def test_missing_link_is_preserved_and_excluded(self):
        self.meta["observer_x_request_id"] = "unknown-observer"
        mapping = self.mapping()
        self.assertEqual(mapping["join"]["unlinked_capture_ids"], ["codex-0001"])
        self.assertFalse(build_report(mapping, self.root)["requests"][0]["usable"])

    def test_log_tampering_fails(self):
        self.log.write_text('{}\n')
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.mapping()

    def test_duplicate_bridge_fails(self):
        row = json.loads(json.dumps(self.wire["requests"][0]))
        row["request_id"] = "other-internal"
        self.wire["requests"].append(row)
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            self.mapping()

    def test_ambiguous_turn_fails(self):
        self.result["turns"].append(self.result["turns"][0])
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.mapping()

    def test_auxiliary_override(self):
        labels = {"codex-0001": {"conversation_id": "side-task", "auxiliary": True,
                                  "evidence": ["manual body review"]}}
        mapping = self.mapping(labels=labels)
        self.assertEqual(build_report(mapping, self.root)["requests"][0]["group"], "auxiliary")

    def test_unknown_backend_cache_usage_stays_unknown(self):
        row = self.wire["requests"][0]
        row["responses"][0]["wrapper"]["data"]["data"]["completion_usage"]["prompt_tokens_details"] = None
        self.assertIsNone(backend_usage(row, "fixture"))

    def test_disagreeing_backend_usage_fails(self):
        row = self.wire["requests"][0]
        second = json.loads(json.dumps(row["responses"][0]))
        second["wrapper"]["data"]["data"]["completion_usage"]["prompt_tokens_details"]["cached_tokens"] = 16
        row["responses"].append(second)
        with self.assertRaisesRegex(ValueError, "changes"):
            backend_usage(row, "fixture")


if __name__ == "__main__":
    unittest.main()
