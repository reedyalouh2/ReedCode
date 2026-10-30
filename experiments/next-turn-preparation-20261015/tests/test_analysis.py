import json
from pathlib import Path
import tempfile
import unittest

from analyze import analyze_trial, block_keys, foreground_ceiling, index_backend, summarize_trial

A, B, C, D, E, X = ([i] * 16 for i in range(1, 7))


def entry(rid, tokens, at, salt="dynamo-cache-salt:trial"):
    return {
        "kind": "engine_enter",
        "request_id": rid,
        "capture_id": rid + "-capture",
        "worker_monotonic_ns": at,
        "cache_salt": salt,
        "prompt_ids": tokens,
    }


def result(rid, at, output=None, cached=0, terminal="finished"):
    return {
        "kind": "engine_result",
        "request_id": rid,
        "worker_monotonic_ns": at,
        "terminal": terminal,
        "output_ids_status": "reported",
        "outputs": {"0": output or []},
        "cached_tokens": cached,
    }


def link(row):
    return {
        "kind": "engine_link",
        "external_request_id": row["request_id"],
        "capture_id": row["capture_id"],
        "engine_request_id": row["request_id"] + "-random",
        "processed_prompt_ids": row["prompt_ids"],
    }


class AttributionTests(unittest.TestCase):
    def test_concurrent_foreground_and_eventual_output_raise_the_ceiling(self):
        events = [
            entry("old", A, 1),
            result("old", 3, X),
            entry("target", A + B + C + D, 10),
            entry("background", A + B, 11),
            result("background", 30, C),
            {
                "kind": "engine_output",
                "request_id": "target",
                "worker_monotonic_ns": 20,
            },
        ]
        index = index_backend(events)
        self.assertEqual(
            foreground_ceiling(
                "target", {"old", "target", "background"}, index[0], index[2], index[3]
            ),
            48,
        )

    def test_unknown_concurrent_output_prevents_credit(self):
        events = [
            entry("old", A, 1),
            entry("target", A + B, 10),
            {
                "kind": "engine_output",
                "request_id": "target",
                "worker_monotonic_ns": 20,
            },
        ]
        index = index_backend(events)
        self.assertIsNone(
            foreground_ceiling(
                "target", {"old", "target"}, index[0], index[2], index[3]
            )
        )

    def test_incomplete_predecessor_prefix_cannot_bound_unobserved_generation(self):
        for terminal in ("cancelled", "consumer_closed", "error", "stream_ended_without_finished"):
            events = [entry("old", A, 1), result("old", 3, B, terminal=terminal),
                      entry("target", A + B + C, 10),
                      {"kind": "engine_output", "request_id": "target", "worker_monotonic_ns": 20}]
            index = index_backend(events)
            with self.subTest(terminal=terminal):
                self.assertIsNone(foreground_ceiling("target", {"old", "target"}, index[0], index[2], index[3]))

    def test_incomplete_predecessor_with_known_divergence_still_bounds_reuse(self):
        events = [entry("old", A, 1), result("old", 3, X, terminal="cancelled"),
                  entry("target", A + B + C, 10),
                  {"kind": "engine_output", "request_id": "target", "worker_monotonic_ns": 20}]
        index = index_backend(events)
        self.assertEqual(foreground_ceiling("target", {"old", "target"}, index[0], index[2], index[3]), 16)

    def test_incomplete_predecessor_can_cover_the_whole_target(self):
        events = [entry("old", A, 1), result("old", 3, B, terminal="cancelled"),
                  entry("target", A + B, 10),
                  {"kind": "engine_output", "request_id": "target", "worker_monotonic_ns": 20}]
        index = index_backend(events)
        self.assertEqual(foreground_ceiling("target", {"old", "target"}, index[0], index[2], index[3]), 32)

    def make_trial(self, root, off=False):
        (root / "manifest.json").write_text(
            json.dumps(
                {
                    "mode": "live",
                    "trial_id": "trial",
                    "condition": "off" if off else "rolling",
                    "background": True,
                }
            )
        )
        (root / "reset.json").write_text(
            json.dumps(
                dict(
                    backend_reset_reported=True,
                    kv_clear_event_verified=True,
                    idle_verified=True,
                )
            )
        )
        normals = [("old", A), ("target", A + B + C + D), ("background", A + B)]
        clients = [
            {
                "client_request_id": rid,
                "session_id": rid,
                "request": {
                    "prompt": tokens,
                    "messages": [{"role": "user", "content": "actual"}],
                },
                "done_event": True,
                "finish_reason": "stop",
            }
            for rid, tokens in normals
        ]
        (root / "normal-requests.json").write_text(json.dumps(clients))
        attempts = [
            {
                "id": "prepare-one",
                "status": "completed",
                "next_dispatch_monotonic_ns": 10,
                "snapshot_ready_monotonic_ns": 4,
                "ended_monotonic_ns": 8,
            },
            {
                "id": "prepare-final",
                "status": "completed",
                "next_dispatch_monotonic_ns": None,
                "snapshot_ready_monotonic_ns": 31,
                "ended_monotonic_ns": 32,
            },
        ]
        (root / "normal-preparations.json").write_text(json.dumps(attempts))
        prep = root / "preparations/s"
        prep.mkdir(parents=True)
        for rid, label, tokens in [
            ("prep", "prepare-one", A + B + C),
            ("final", "prepare-final", A + E),
        ]:
            (prep / (rid + ".json")).write_text(
                json.dumps(
                    {
                        "client_request_id": rid,
                        "label": label,
                        "status": "completed",
                        "request": {"prompt": tokens},
                    }
                )
            )
        (root / "events.jsonl").write_text(
            "".join(
                json.dumps({"kind": "http_request_link", "client_request_id": rid})
                + "\n"
                for rid in ["old", "target", "background", "prep", "final"]
            )
        )
        events = [
            entry("old", A, 1),
            result("old", 3, X),
            entry("prep", A + B + C, 4),
            result("prep", 8, [9], 16),
            entry("target", A + B + C + D, 10),
            entry("background", A + B, 11),
            result("background", 30, [], 16),
            {
                "kind": "engine_output",
                "request_id": "target",
                "worker_monotonic_ns": 20,
            },
            result("target", 21, [8], 48),
            {"kind": "engine_output", "request_id": "old", "worker_monotonic_ns": 2},
            {
                "kind": "engine_output",
                "request_id": "background",
                "worker_monotonic_ns": 25,
            },
            entry("final", A + E, 31),
            result("final", 32, [7], 16),
        ]
        events += [link(row) for row in events if row["kind"] == "engine_enter"]
        return index_backend(events)

    def test_credit_requires_actual_cache_and_final_unused_branch_is_retained(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            report = analyze_trial(root, self.make_trial(root))
        self.assertTrue(report["attribution_valid"], report["errors"])
        target = next(
            r for r in report["normal_requests"] if r["request_id"] == "target"
        )
        self.assertEqual(target["foreground_history_ceiling_tokens"], 32)
        self.assertEqual(target["preparation_credit_lower_bound_tokens"], 16)
        self.assertEqual(
            report["completed_preparation_blocks_not_matched_by_later_real_input"], 1
        )
        self.assertIsNone(report["all_attempts"][-1]["next_dispatch_monotonic_ns"])
        self.assertTrue(
            all(
                r["computed_partial_prefill_tokens"] is None
                for r in report["preparation_requests"]
            )
        )

    def test_off_with_unexplained_extra_cache_fails_attribution(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            report = analyze_trial(root, self.make_trial(root, off=True))
        self.assertFalse(report["attribution_valid"])
        self.assertIsNone(report["preparation_credit_lower_bound_tokens"])

    def test_changed_backend_input_fails_attribution(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            index = self.make_trial(root)
            index[0]["prep"]["prompt_ids"] = A + E
            report = analyze_trial(root, index)
        self.assertFalse(report["attribution_valid"])
        self.assertTrue(any("token parity" in x for x in report["errors"]))

    def test_unmapped_client_salt_is_rejected_at_backend(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            index = self.make_trial(root)
            index[0]["prep"]["cache_salt"] = "trial"
            report = analyze_trial(root, index)
        self.assertFalse(report["attribution_valid"])
        self.assertTrue(any("cache salt mismatch" in x for x in report["errors"]))

    def test_prefix_block_identity_includes_history_and_salt(self):
        self.assertNotEqual(block_keys(A + B, "s")[-1], block_keys(C + B, "s")[-1])
        self.assertNotEqual(block_keys(A, "s"), block_keys(A, "other"))

    def test_failed_turn_and_explicit_response_issue_survive_summary(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            index = self.make_trial(root)
            path = root / "normal-requests.json"
            clients = json.loads(path.read_text())
            clients[0]["response_issue"] = "empty_response"
            path.write_text(json.dumps(clients))
            turns = [{"turn": 0, "calls": 1, "outcome": "empty_response"},
                     {"turn": 1, "calls": 1, "outcome": "assistant_finished"}]
            (root / "sessions").mkdir()
            (root / "sessions/old.json").write_text(json.dumps({
                "session_id": "old", "task_id": "task", "turns": turns,
                "outcome": "finished_with_turn_failures", "model_calls": 2,
                "tool_calls": 0, "capture_flush": {},
            }))
            report = analyze_trial(root, index)
            summary = summarize_trial(report)
        old = next(row for row in report["normal_requests"] if row["request_id"] == "old")
        self.assertEqual(old["response_issue"], "empty_response")
        self.assertEqual(old["finish_reason"], "stop")
        self.assertEqual(summary["sessions"][0]["turns"], turns)
        self.assertEqual(summary["sessions"][0]["outcome"], "finished_with_turn_failures")

    def test_older_capture_uses_ineligible_attempt_reason(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            index = self.make_trial(root)
            path = root / "normal-preparations.json"
            attempts = json.loads(path.read_text())
            attempts.append({"id": "old-invalid", "trigger_request_id": "old",
                             "status": "ineligible", "reason": "empty_response"})
            path.write_text(json.dumps(attempts))
            report = analyze_trial(root, index)
        old = next(row for row in report["normal_requests"] if row["request_id"] == "old")
        self.assertEqual(old["response_issue"], "empty_response")
        self.assertEqual(len(report["normal_requests"]), 3)


if __name__ == "__main__":
    unittest.main()
