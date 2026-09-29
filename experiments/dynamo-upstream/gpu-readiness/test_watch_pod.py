import tempfile
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from watch_pod import action, delete_and_verify, monitor, reconcile
from study_cleanup import cleanup, identify_created, run_study


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def command(self, *args):
        self.calls.append(args)
        return next(self.responses)


class WatchdogTests(unittest.TestCase):
    def test_readiness_and_finish_deadlines(self):
        args = dict(ready=False, finished=False, rate=1.59, disk_gb=150)
        self.assertEqual(action(2099, **args)[0], "wait")
        self.assertEqual(action(2100, **args), ("delete", "readiness_deadline"))
        args["ready"] = True
        self.assertEqual(action(7800, **args)[0], "stop_requests")
        self.assertEqual(action(8400, **args), ("delete", "teardown_deadline"))
        args["finished"] = True
        self.assertEqual(action(300, **args), ("delete", "study_finished"))

    def test_invalid_quotes_fail_closed(self):
        for rate in [0, -1, 1.66, float("nan"), float("inf")]:
            self.assertEqual(action(0, ready=True, finished=False, rate=rate, disk_gb=150)[0], "delete")
        self.assertEqual(action(0, ready=True, finished=False, rate=1.59, disk_gb=151)[0], "delete")

    def test_delete_success_requires_absence(self):
        c = Client([({}, None), ([{"id": "ours"}], None)])
        self.assertEqual(delete_and_verify(c, "ours"), (False, "deletion_pending"))
        c = Client([({}, None), ([{"id": "unrelated"}], None)])
        self.assertEqual(delete_and_verify(c, "ours"), (True, "absent_from_pod_list"))

    def test_unknown_listing_does_not_mean_deleted(self):
        for listing in [None, {}, [{}]]:
            c = Client([({}, None), (listing, None)])
            self.assertFalse(delete_and_verify(c, "ours")[0])

    def test_network_failure_keeps_cleanup_pending(self):
        c = Client([(None, "network_error")])
        self.assertEqual(delete_and_verify(c, "ours"), (False, "network_error"))

    def test_existing_approval_does_not_approve_changed_guard(self):
        with tempfile.TemporaryDirectory() as path:
            with self.assertRaisesRegex(ValueError, "approval"):
                monitor({}, Path(path), Client([]))

    def test_clock_rollback_does_not_extend_rental(self):
        state = {"local_guard_approved": True, "pod_id": "ours", "pod_name": "study",
                 "allocation_started_unix": 0, "compute_rate_per_hour": 1.59, "disk_gb": 150}
        walls = iter([8399, 8399, 8399, 8399, 100, 100])
        monos = iter([0, 0, 2])
        c = Client([({"id": "ours", "name": "study", "costPerHr": 1.59}, None),
                    ({}, None), ([], None)])
        with tempfile.TemporaryDirectory() as path:
            directory = Path(path)
            (directory / "ready").touch()
            monitor(state, directory, c, wall=lambda: next(walls),
                    monotonic=lambda: next(monos), sleep=lambda _: None)
            self.assertTrue((directory / "deleted").exists())
        self.assertEqual(c.calls[-2:], [("pod", "delete", "ours"), ("pod", "list")])

    def test_ambiguous_creation_reconciles_only_exact_study_name(self):
        c = Client([([{"id": "ours", "name": "unique-study"},
                      {"id": "theirs", "name": "another-study"}], None)])
        self.assertEqual(identify_created(c, "unique-study"), ["ours"])

    def test_controller_retries_cleanup(self):
        c = Client([(None, "network_error"), ({}, None), ([], None)])
        waits = []
        with tempfile.TemporaryDirectory() as path:
            cleanup(c, ["ours"], Path(path), sleep=waits.append)
            self.assertTrue((Path(path) / "controller_deleted").exists())
        self.assertEqual(waits, [10])

    def test_study_failure_still_deletes(self):
        c = Client([({"id": "ours", "name": "study"}, None), ({}, None), ([], None)])
        state = {"local_guard_approved": True, "pod_id": "ours", "pod_name": "study",
                 "allocation_started_unix": 100}
        def fail(*args, **kwargs):
            raise RuntimeError("simulated study failure")
        with tempfile.TemporaryDirectory() as path, patch("study_cleanup.time.time", return_value=101):
            directory = Path(path)
            (directory / "watchdog-heartbeat.json").write_text(
                json.dumps({"pod_id": "ours", "pid": os.getpid(), "at_unix": 101}))
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                run_study(c, state, directory, ["unused-test-command"], run=fail, sleep=lambda _: None)
            self.assertTrue((directory / "controller_deleted").exists())
            self.assertTrue((directory / "stop_requests").exists())

    def test_stale_watchdog_prevents_study_and_triggers_cleanup(self):
        c = Client([({"id": "ours", "name": "study"}, None), ({}, None), ([], None)])
        state = {"local_guard_approved": True, "pod_id": "ours", "pod_name": "study",
                 "allocation_started_unix": 100}
        def must_not_run(*args, **kwargs):
            self.fail("Study started with a stale watchdog")
        with tempfile.TemporaryDirectory() as path, patch("study_cleanup.time.time", return_value=200):
            directory = Path(path)
            (directory / "watchdog-heartbeat.json").write_text(
                json.dumps({"pod_id": "ours", "pid": os.getpid(), "at_unix": 100}))
            with self.assertRaisesRegex(ValueError, "stale"):
                run_study(c, state, directory, ["unused-test-command"], run=must_not_run, sleep=lambda _: None)
            self.assertTrue((directory / "controller_deleted").exists())

    def test_identity_mismatch_latches_cleanup_for_known_allocation(self):
        state = {"local_guard_approved": True, "pod_id": "ours", "pod_name": "study",
                 "allocation_started_unix": 0, "compute_rate_per_hour": 1.59, "disk_gb": 150}
        client = Client([({"id": "unrelated", "name": "wrong"}, None), ({}, None),
                         ([{"id": "unrelated", "name": "wrong"}], None)])
        with tempfile.TemporaryDirectory() as path:
            monitor(state, Path(path), client, wall=lambda: 1, monotonic=lambda: 1, sleep=lambda _: None)
            self.assertTrue((Path(path) / "deleted").exists())
        self.assertIn(("pod", "delete", "ours"), client.calls)
        self.assertNotIn(("pod", "delete", "unrelated"), client.calls)

    def test_reconciliation_survives_api_outage_past_readiness_and_uses_one_monitor(self):
        intent = {"local_guard_approved": True, "pod_name": "reedcode-combined-unique",
                  "allocation_started_unix": 0, "compute_rate_per_hour": 1.59, "disk_gb": 150}
        client = Client([(None, "network_error"), (None, "network_error"),
                         ([{"id": "theirs", "name": "other-study"}], None),
                         ([{"id": "ours", "name": intent["pod_name"]}], None)])
        waits = []
        with tempfile.TemporaryDirectory() as path, patch("watch_pod.monitor") as adopted:
            reconcile(intent, Path(path), client, wall=lambda: 4000, sleep=waits.append)
            self.assertEqual(adopted.call_count, 1)
            self.assertEqual(adopted.call_args.args[0]["pod_id"], "ours")
            self.assertTrue((Path(path) / "reconciliation-heartbeat.json").exists())
        self.assertEqual(waits, [10, 10, 10])
        self.assertTrue(all(call == ("pod", "list") for call in client.calls))

    def test_multiple_same_name_results_clean_only_that_exact_name(self):
        intent = {"local_guard_approved": True, "pod_name": "reedcode-combined-unique"}
        client = Client([([{"id": "ours1", "name": intent["pod_name"]},
                           {"id": "ours2", "name": intent["pod_name"]},
                           {"id": "theirs", "name": "unrelated"}], None)])
        with tempfile.TemporaryDirectory() as path, patch("study_cleanup.cleanup") as cleaned:
            reconcile(intent, Path(path), client, wall=lambda: 4000, sleep=lambda _: None)
            self.assertEqual(cleaned.call_args.args[1], ["ours1", "ours2"])


if __name__ == "__main__":
    unittest.main()
