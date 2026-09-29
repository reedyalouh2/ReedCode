import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import create_transfer


CATALOG = {"cpus": [{"id": "cpu3c", "availability": "HIGH", "vcpu": {"min": 2, "max": 32},
                     "ramGbPerVcpu": 2, "price": {"securePerVcpu": 0.03}}]}


class Client:
    def __init__(self):
        self.calls = []

    def command(self, *args):
        self.calls.append(args)
        if args == ("user",):
            return {"clientBalance": 8, "currentSpendPerHr": 0}, None
        if args == ("pod", "list"):
            return [], None
        if args[:2] == ("pod", "delete"):
            return {}, None
        raise AssertionError(args)


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.approval = self.root / "approval.json"
        self.approval.write_text(json.dumps({"approved": True, "cpu_transfer_approved": True,
            "local_guard_approved": True, "total_cost_cap_usd": 5, "helper_cost_cap_usd": 0.10,
            "resources_created": 0}))
        self.args = argparse.Namespace(approval=self.approval, key_file=self.root / "unused-key",
            cli=self.root / "unused-cli", output=self.root / "allocation", execute=True)
        self.client = Client()
        self.posts = []
        for target, value in (("Runpod", self.client), ("private_key", "test-only-value")):
            mock = patch.object(create_transfer, target, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def guard(self, command, output):
        self.assertIn("--intent", command)
        self.assertFalse((self.args.output / "creation_submitted").exists())
        for filename in ("reconciliation-heartbeat.json", "watchdog-heartbeat.json"):
            (self.args.output / filename).write_text("{}")

    def api(self, key, method, path, body=None):
        if method == "GET":
            return CATALOG
        self.assertEqual((method, path), ("POST", "/v2/pods"))
        self.assertTrue((self.args.output / "reconciliation-heartbeat.json").exists())
        self.posts.append(body)
        return {**body, "id": "fixture123", "cost": 0.06,
                "cpu": {"id": "cpu3c", "vcpuCount": 2, "memory": 4}}

    def test_exact_public_body_has_no_gpu_ports_or_credentials(self):
        body = create_transfer.body_for("fixture")
        self.assertEqual(body["env"], {})
        self.assertEqual(body["ports"], [])
        self.assertNotIn("gpu", body)
        self.assertEqual(body["disk"], 10)
        self.assertEqual(body["cmd"], create_transfer.COPY_ARGS)
        self.assertEqual(create_transfer.quote_rate(CATALOG), 0.06)

    def test_guard_precedes_single_create_and_approval_is_consumed(self):
        with patch.object(create_transfer, "api", side_effect=self.api), \
                patch.object(create_transfer, "detached", side_effect=self.guard):
            create_transfer.create(self.args)
        self.assertEqual(len(self.posts), 1)
        state = json.loads((self.args.output / "state.json").read_text())
        self.assertTrue(state["never_mark_ready"])
        self.assertLess(state["estimated_35_minute_cost_at_ceiling"], 0.06)
        self.assertFalse((self.args.output / "ready").exists())
        with self.assertRaisesRegex(ValueError, "unused CPU-transfer approval"):
            create_transfer.create(self.args)

    def test_uncertain_post_keeps_reconciliation_and_no_retry(self):
        def uncertain(key, method, path, body=None):
            if method == "POST":
                self.posts.append(body)
                raise RuntimeError("simulated transport failure")
            return CATALOG
        with patch.object(create_transfer, "api", side_effect=uncertain), \
                patch.object(create_transfer, "detached", side_effect=self.guard):
            with self.assertRaises(RuntimeError):
                create_transfer.create(self.args)
        self.assertEqual(len(self.posts), 1)
        self.assertTrue((self.args.output / "finished").exists())
        self.assertTrue((self.args.output / "uncertain-creation.json").exists())

    def test_missing_cpu_approval_prevents_private_api_calls(self):
        self.approval.write_text('{"approved":true}')
        with patch.object(create_transfer, "api") as call:
            with self.assertRaises(ValueError):
                create_transfer.create(self.args)
            call.assert_not_called()

    def test_resource_mismatch_deletes_only_known_new_pod(self):
        def mismatch(*args):
            pod = self.api(*args)
            if args[1] == "POST":
                pod["disk"] = 20
            return pod
        with patch.object(create_transfer, "api", side_effect=mismatch), \
                patch.object(create_transfer, "detached", side_effect=self.guard):
            with self.assertRaisesRegex(ValueError, "differs: disk"):
                create_transfer.create(self.args)
        self.assertIn(("pod", "delete", "fixture123"), self.client.calls)

    def test_wrong_name_does_not_delete_unrelated_id(self):
        def mismatch(*args):
            pod = self.api(*args)
            if args[1] == "POST":
                pod["name"] = "unrelated"
            return pod
        with patch.object(create_transfer, "api", side_effect=mismatch), \
                patch.object(create_transfer, "detached", side_effect=self.guard):
            with self.assertRaisesRegex(RuntimeError, "detached guard keeps"):
                create_transfer.create(self.args)
        self.assertFalse(any(call[:2] == ("pod", "delete") for call in self.client.calls))


if __name__ == "__main__":
    unittest.main()
