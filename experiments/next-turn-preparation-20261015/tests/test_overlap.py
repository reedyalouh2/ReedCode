import json
from pathlib import Path
import tempfile
import unittest

from overlap import native_window, overlap, trial_overlap, union


class OverlapTests(unittest.TestCase):
    def test_overlapping_background_windows_are_not_double_counted(self):
        self.assertEqual(union([(5, 20), (10, 15), (19, 25)]), [(5, 25)])
        self.assertEqual(overlap((0, 30), [(5, 20), (10, 15), (19, 25)]), 20)

    def test_touching_windows_have_no_overlap(self):
        self.assertEqual(overlap((10, 20), [(0, 10), (20, 30)]), 0)

    def test_missing_native_timestamps_stay_unknown(self):
        for stats in ({}, {"scheduled_ts": 1}, {"scheduled_ts": 0, "last_token_ts": 2},
                      {"scheduled_ts": float("nan"), "last_token_ts": 2},
                      {"scheduled_ts": 3, "last_token_ts": 2}):
            self.assertIsNone(native_window({"engine_stats": stats}))
        self.assertIsNone(overlap(None, [(1, 2)]))
        self.assertEqual(native_window({"engine_stats": {"scheduled_ts": 1, "last_token_ts": 2}}), (10**9, 2 * 10**9))

    def test_configured_background_without_overlap_and_failed_turns(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / "manifest.json").write_text(json.dumps({"trial_id": "trial", "condition": "rolling", "background": True}))
            (path / "sessions").mkdir()
            turns = [{"outcome": "empty_response"}]
            for rid, task in (("t", "target"), ("b", "background")):
                (path / f"{rid}-requests.json").write_text(json.dumps([{"client_request_id": rid, "session_id": rid}]))
                (path / f"sessions/{rid}.json").write_text(json.dumps({"session_id": rid, "task_id": task, "outcome": "finished_with_turn_failures", "turns": turns}))
            entries = {"t": {"worker_monotonic_ns": 0}, "b": {"worker_monotonic_ns": 3 * 10**9}}
            results = {"t": {"worker_monotonic_ns": 2 * 10**9, "engine_stats": {"scheduled_ts": 1, "last_token_ts": 2}},
                       "b": {"worker_monotonic_ns": 5 * 10**9, "engine_stats": {"scheduled_ts": 3, "last_token_ts": 4}}}
            report = trial_overlap(path, entries, results, "target")
        self.assertTrue(report["background_configured"])
        self.assertEqual(report["summary"]["target_native"]["requests_with_observed_overlap"], 0)
        self.assertEqual(report["sessions"][0]["turns"], turns)


if __name__ == "__main__":
    unittest.main()
