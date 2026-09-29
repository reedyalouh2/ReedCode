import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from report import build_report, response_usage, resolve_causes


ROOT = Path(__file__).resolve().parent


def event(kind, **values):
    return ("event: " + kind + "\ndata: " + json.dumps({"type": kind, **values}) + "\n\n").encode()


def response(total, cached, status="completed"):
    usage = {"input_tokens": total, "output_tokens": 3}
    if cached is not None:
        usage["input_tokens_details"] = {"cached_tokens": cached}
    return event("response." + status, response={"status": status, "usage": usage})


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.requests = []
        self.mapping = {"block_size": 16, "sessions": [{"id": "session", "protocol": "responses",
                                                       "requests": self.requests}]}

    def tearDown(self):
        self.temp.cleanup()

    def add(self, tokens, cached, turn=1, epoch="epoch1", conversation="main", response_body=None):
        index = len(self.requests) + 1
        folder = self.base / str(index)
        folder.mkdir()
        body = response_body if response_body is not None else response(len(tokens), cached)
        request = b'{"model":"test"}'
        (folder / "request.body").write_bytes(request)
        (folder / "response.body").write_bytes(body)
        meta = {"complete": True, "response_status": 200, "started_monotonic_ns": index * 10,
                "ended_monotonic_ns": index * 10 + 1,
                "request_sha256": hashlib.sha256(request).hexdigest(),
                "response_sha256": hashlib.sha256(body).hexdigest()}
        (folder / "metadata.json").write_text(json.dumps(meta))
        backend_raw = json.dumps({"input": {"ids": tokens}}).encode()
        (folder / "backend.json").write_bytes(backend_raw)
        entry = {"id": "client-" + str(index), "user_turn": turn, "conversation_id": conversation,
                 "capture": str(index), "link_evidence": ["fixture/controller-link.json"],
                 "backend": {"path": f"{index}/backend.json", "sha256": hashlib.sha256(backend_raw).hexdigest(),
                             "token_ids_pointer": "/input/ids", "request_id": "unrelated-backend-" + str(index),
                             "worker_id": "worker", "epoch": epoch}}
        self.requests.append(entry)
        return entry

    def test_block_rounding_signed_gap_and_token_weighted_aggregates(self):
        self.add(list(range(100)), 0)
        self.add(list(range(95)) + list(range(300, 305)), 96)
        self.add(list(range(64)), 32, turn=2)
        report = build_report(self.mapping, self.base)
        first, second, third = report["requests"]
        self.assertEqual([row["ideal_tokens"] for row in report["requests"]], [0, 80, 64])
        self.assertEqual(second["gap"], -0.16)
        self.assertEqual(third["group"], "cross_turn")
        self.assertEqual(third["compatible_misses"], [{"start": 32, "end": 64, "cause": "other",
            "explanation": "No evidence assigns this compatible-prefix miss to a cause; engine boundary unverified",
            "evidence": []}])
        stats = report["sessions"][0]["all"]
        self.assertEqual(stats["actual_reuse"], 128 / 264)
        self.assertEqual(stats["ideal_reuse"], 144 / 264)
        self.assertEqual(stats["gap"], 16 / 264)
        self.assertEqual(stats["cached_over_ideal"], 128 / 144)
        self.assertIsNone(first["eligible_ideal_tokens"])

    def test_last_token_boundary_requires_explicit_verified_rule(self):
        self.add(list(range(32)), 0)
        self.add(list(range(32)), 16)
        self.mapping["engine_boundary"] = {"rule": "last_token_recomputed", "evidence": ["pinned-source:42"]}
        row = build_report(self.mapping, self.base)["requests"][1]
        self.assertEqual(row["ideal_tokens"], 32)
        self.assertEqual(row["eligible_ideal_tokens"], 16)
        self.assertEqual(row["eligible_gap"], 0)

    def test_epoch_conversation_and_overlap_exclude_references(self):
        self.add(list(range(32)), 0)
        self.add(list(range(32)), 0, epoch="restart")
        self.add(list(range(32)), 0, conversation="another")
        self.add(list(range(32)), 0)
        meta_path = self.base / "1/metadata.json"
        meta = json.loads(meta_path.read_text())
        meta["ended_monotonic_ns"] = 50
        meta_path.write_text(json.dumps(meta))
        rows = build_report(self.mapping, self.base)["requests"]
        self.assertEqual([row["ideal_tokens"] for row in rows], [0, 0, 0, 0])

    def test_missing_link_or_bad_hash_stays_visible(self):
        first = self.add(list(range(32)), 0)
        first.pop("link_evidence")
        second = self.add(list(range(32)), 0)
        second["backend"]["sha256"] = "bad"
        report = build_report(self.mapping, self.base)
        self.assertEqual(report["sessions"][0]["all"]["requests"], 2)
        self.assertEqual(report["sessions"][0]["all"]["usable_requests"], 0)
        self.assertIsNone(report["sessions"][0]["all"]["actual_reuse"])
        self.assertTrue(all(not row["usable"] for row in report["requests"]))

    def test_missing_cache_usage_is_unknown(self):
        self.add(list(range(32)), None)
        row = build_report(self.mapping, self.base)["requests"][0]
        self.assertFalse(row["usable"])
        self.assertIsNone(row["cached_tokens"])

    def test_output_limit_response_retains_usage_and_reference(self):
        self.add(list(range(32)), 16, response_body=response(32, 16, "incomplete"))
        self.add(list(range(32)), 0)
        rows = build_report(self.mapping, self.base)["requests"]
        self.assertEqual(rows[0]["cached_tokens"], 16)
        self.assertTrue(rows[0]["usable"])
        self.assertEqual(rows[1]["ideal_tokens"], 32)

    def test_cause_ranges_require_evidence_and_fill_only_known_miss_interval(self):
        self.add(list(range(64)), 0)
        self.add(list(range(64)), 16)
        self.mapping["causes"] = {"client-2": {"compatible_misses": [{"start": 32, "end": 48,
            "cause": "eviction", "explanation": "Fixture stores then removes the matching blocks",
            "evidence": ["fixture/events.jsonl:3-6"]}]}}
        report = build_report(self.mapping, self.base)
        self.assertEqual(report["cause_rankings"]["compatible_misses"], {"other": 32, "eviction": 16})
        self.mapping["causes"]["client-2"]["compatible_misses"][0].pop("evidence")
        with self.assertRaises(ValueError):
            build_report(self.mapping, self.base)
        with self.assertRaises(ValueError):
            resolve_causes(16, 64, [{"start": 0, "end": 32}], "missing")

    def test_anthropic_final_usage_replaces_initial_estimate(self):
        raw = (event("message_start", message={"usage": {"input_tokens": 999}})
               + event("message_delta", delta={"stop_reason": "end_turn"}, usage={"input_tokens": 60,
                       "cache_read_input_tokens": 20, "cache_creation_input_tokens": 4, "output_tokens": 3})
               + event("message_stop"))
        value = response_usage(raw, "anthropic")
        self.assertEqual(value["input_tokens"], 84)
        self.assertEqual(value["cached_tokens"], 20)
        self.assertEqual(value["output_tokens"], 3)

    def test_anthropic_omission_can_be_resolved_only_by_mapped_backend_usage(self):
        raw = (event("message_delta", delta={"stop_reason": "end_turn"}, usage={"input_tokens": 32,
                    "cache_creation_input_tokens": 0, "output_tokens": 3}) + event("message_stop"))
        entry = self.add(list(range(32)), 0, response_body=raw)
        self.mapping["sessions"][0]["protocol"] = "anthropic"
        self.assertFalse(build_report(self.mapping, self.base)["requests"][0]["usable"])
        entry["backend_usage"] = {"input_tokens": 32, "cached_tokens": 0, "evidence": ["backend-usage:1"]}
        row = build_report(self.mapping, self.base)["requests"][0]
        self.assertTrue(row["usable"])
        self.assertEqual(row["cached_tokens"], 0)
        self.assertIsNone(row["usage"]["cached_tokens"])

    def test_saved_real_cli_stubs_parse_without_inventing_backend_evidence(self):
        fixtures = [("verified/claude", "anthropic"), ("coding-tools-seccomp/codex", "responses")]
        for folder, protocol in fixtures:
            for path in (ROOT / "readiness" / folder / "capture").glob("*/response.body"):
                with self.subTest(path=path):
                    usage = response_usage(path.read_bytes(), protocol)
                    self.assertEqual((usage["input_tokens"], usage["cached_tokens"]), (100, 20))
                    self.assertEqual(usage["terminal_status"], "completed")

    def test_actual_stock_frontend_sse_shows_omitted_anthropic_zero(self):
        archives = ROOT.parent / "001-speculative-prefill/linux-readiness"
        cases = [("stock-protocol-evidence.tar.gz", "protocol-attempt2/claude-1-response.body", "anthropic", None),
                 ("stock-codex-protocol-evidence.tar.gz", "protocol-codex/codex-1-response.body", "responses", 0)]
        for archive, filename, protocol, cached in cases:
            with tarfile.open(archives / archive) as bundle:
                usage = response_usage(bundle.extractfile(filename).read(), protocol)
            self.assertEqual(usage["terminal_status"], "completed")
            self.assertEqual(usage["cached_tokens"], cached)
            self.assertEqual(usage["input_tokens"], 8696 if protocol == "responses" else None)

    def test_unterminated_sse_event_is_not_a_completed_capture(self):
        usage = response_usage(response(32, 0).rstrip(), "responses")
        self.assertIsNone(usage["terminal_status"])
        self.assertTrue(usage["warnings"])


if __name__ == "__main__":
    unittest.main()
