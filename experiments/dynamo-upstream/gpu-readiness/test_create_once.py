import argparse
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import create_once


class FakeRunpod:
    def __init__(self, *_, actual_rate=1.59, uncertain=False):
        self.calls, self.pods = [], []
        self.actual_rate, self.uncertain = actual_rate, uncertain

    def command(self, *args):
        self.calls.append(args)
        if args == ("user",):
            return {"clientBalance": 9.13, "currentSpendPerHr": 0}, None
        if args[:2] == ("pod", "list"):
            return list(self.pods), None
        if args[:2] == ("gpu", "list"):
            return [{"gpuId": create_once.GPU, "available": True, "secureCloud": True,
                     "securePricePerHr": 1.59}], None
        if args[:2] == ("pod", "create"):
            pod = {"id": "test123", "name": args[args.index("--name") + 1],
                   "costPerHr": self.actual_rate, "imageName": args[args.index("--image") + 1],
                   "containerDiskInGb": 150, "volumeInGb": 0, "gpuCount": 1}
            self.pods = [pod]
            return (None, "network_error") if self.uncertain else (pod, None)
        if args[:2] == ("pod", "delete"):
            self.pods = []
            return {}, None
        raise AssertionError(args)


class CreationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        artifact = self.root / "artifact"
        artifact.write_text("test artifact")
        (self.root / "approval.json").write_text(json.dumps({"approved": True,
            "local_guard_approved": True, "previous_key_revoked_user_confirmed": True,
            "resources_created": 0, "total_cost_cap_usd": 5}))
        (self.root / "bootstrap_ssh.py").write_text("pass\n")
        self.gates = self.root / "gates.json"
        self.gates.write_text(json.dumps({"gates": dict.fromkeys(create_once.GATES, True),
            "artifacts": [{"path": str(artifact),
                           "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}]}))
        public = self.root / "study.pub"
        public.write_text("ssh-ed25519 fixture")
        self.args = argparse.Namespace(gates=self.gates, ssh_public_key=public, execute=True,
            output=self.root / "allocation", cli=self.root / "fake-cli", key_file=self.root / "no-key")
        self.here = patch.object(create_once, "HERE", self.root)
        self.here.start()
        self.addCleanup(self.here.stop)

    def arm_fake(self, command, output):
        if any("watch_pod.py" in value for value in command):
            self.assertIn("--intent", command)
            self.assertFalse((self.args.output / "creation_submitted").exists())
            (self.args.output / "reconciliation-heartbeat.json").write_text("{}")
            (self.args.output / "watchdog-heartbeat.json").write_text("{}")
        else:
            (self.args.output / "controller-heartbeat.json").write_text("{}")

    def test_changed_artifact_prevents_any_api_call(self):
        (self.root / "artifact").write_text("changed")
        with patch.object(create_once, "Runpod") as client:
            with self.assertRaisesRegex(ValueError, "artifact changed"):
                create_once.create(self.args)
            client.assert_not_called()

    def test_one_creation_arms_guards_and_consumes_approval(self):
        client = FakeRunpod()
        with patch.object(create_once, "Runpod", return_value=client), \
                patch.object(create_once, "detached", side_effect=self.arm_fake):
            create_once.create(self.args)
        self.assertEqual(sum(call[:2] == ("pod", "create") for call in client.calls), 1)
        state = json.loads((self.args.output / "state.json").read_text())
        self.assertEqual(state["pod_id"], "test123")
        with self.assertRaisesRegex(ValueError, "one-rental approval"):
            create_once.validate_gates(self.gates)

    def test_uncertain_creation_leaves_detached_cleanup_armed_and_never_retries(self):
        client = FakeRunpod(uncertain=True)
        with patch.object(create_once, "Runpod", return_value=client), \
                patch.object(create_once, "detached", side_effect=self.arm_fake) as spawn:
            with self.assertRaisesRegex(RuntimeError, "detached guard keeps"):
                create_once.create(self.args)
            self.assertEqual(spawn.call_count, 1)
        self.assertEqual(sum(call[:2] == ("pod", "create") for call in client.calls), 1)
        self.assertTrue((self.args.output / "finished").exists())
        self.assertTrue((self.args.output / "creation_submitted").exists())
        self.assertEqual(json.loads((self.args.output / "uncertain-creation.json").read_text())["status"],
                         "detached_exact_name_cleanup_pending")

    def test_changed_rate_deletes_without_starting_study(self):
        client = FakeRunpod(actual_rate=2.0)
        with patch.object(create_once, "Runpod", return_value=client), \
                patch.object(create_once, "detached", side_effect=self.arm_fake) as spawn:
            with self.assertRaisesRegex(ValueError, "outside the budget"):
                create_once.create(self.args)
            self.assertEqual(spawn.call_count, 1)
        self.assertEqual(client.pods, [])

    def test_guard_start_failure_prevents_creation(self):
        client = FakeRunpod()
        with patch.object(create_once, "Runpod", return_value=client), \
                patch.object(create_once, "detached", side_effect=OSError("simulated spawn failure")):
            with self.assertRaises(OSError):
                create_once.create(self.args)
        self.assertFalse(any(call[:2] == ("pod", "create") for call in client.calls))
        self.assertTrue((self.args.output / "creation_aborted_before_submission").exists())

    def replacement_approval(self):
        original = self.root / "approval.json"
        approval = json.loads(original.read_text())
        original.write_text(json.dumps({**approval, "resources_created": 1, "creation_attempted": True}))
        self.args.approval = self.root / "replacement-approval.json"
        approval.update(approved_image="ghcr.io/example/study-runtime@sha256:" + "a" * 64,
                        prior_cost_reserve_usd=0.17)
        self.args.approval.write_text(json.dumps(approval))
        return approval

    def test_replacement_uses_distinct_approval_and_keeps_prior_cost(self):
        approval = self.replacement_approval()
        original = (self.root / "approval.json").read_bytes()
        client = FakeRunpod()
        with patch.object(create_once, "Runpod", return_value=client), \
                patch.object(create_once, "detached", side_effect=self.arm_fake):
            create_once.create(self.args)
        self.assertEqual((self.root / "approval.json").read_bytes(), original)
        creation = next(call for call in client.calls if call[:2] == ("pod", "create"))
        self.assertEqual(creation[creation.index("--image") + 1], approval["approved_image"])
        state = json.loads((self.args.output / "state.json").read_text())
        self.assertEqual(state["prior_cost_reserve_usd"], 0.17)
        self.assertEqual(state["setup_cost_reserve_usd"], 0)
        self.assertAlmostEqual(state["remaining_budget_usd"], 4.83)
        self.assertEqual(state["total_cost_cap_usd"], 5)
        with self.assertRaisesRegex(ValueError, "one-rental approval"):
            create_once.validate_gates(self.gates, self.args.approval)

    def test_unpinned_image_prevents_any_api_call(self):
        approval = self.replacement_approval()
        for image in ("ghcr.io/example/runtime:latest", "repository@sha256:" + "a" * 63,
                      "repository@sha256:" + "g" * 64, None):
            with self.subTest(image=image):
                self.args.approval.write_text(json.dumps({**approval, "approved_image": image}))
                with patch.object(create_once, "Runpod") as client:
                    with self.assertRaisesRegex(ValueError, "pinned SHA-256"):
                        create_once.create(self.args)
                    client.assert_not_called()

    def test_invalid_prior_reserve_prevents_any_api_call(self):
        approval = self.replacement_approval()
        for reserve in (-0.01, 0.18, float("nan"), float("inf"), True, "0.17"):
            with self.subTest(reserve=reserve):
                self.args.approval.write_text(json.dumps({**approval, "prior_cost_reserve_usd": reserve}))
                with patch.object(create_once, "Runpod") as client:
                    with self.assertRaisesRegex(ValueError, "Prior cost reserve"):
                        create_once.create(self.args)
                    client.assert_not_called()

    def test_setup_reserve_is_kept_in_intent_and_remaining_budget(self):
        approval = self.replacement_approval()
        approval["setup_cost_reserve_usd"] = 0.10
        self.args.approval.write_text(json.dumps(approval))
        client = FakeRunpod()
        with patch.object(create_once, "Runpod", return_value=client), \
                patch.object(create_once, "detached", side_effect=self.arm_fake):
            create_once.create(self.args)
        for name in ("creation-intent.json", "state.json"):
            state = json.loads((self.args.output / name).read_text())
            self.assertEqual(state["setup_cost_reserve_usd"], 0.10)
            self.assertEqual(state["prior_cost_reserve_usd"], 0.17)
            self.assertAlmostEqual(state["remaining_budget_usd"], 4.73)

    def test_dry_run_deducts_both_reserves_without_contacting_api(self):
        approval = self.replacement_approval()
        approval["setup_cost_reserve_usd"] = 0.10
        self.args.approval.write_text(json.dumps(approval))
        self.args.execute = False
        with patch.object(create_once, "Runpod") as client, patch("sys.stdout", new_callable=io.StringIO) as output:
            create_once.create(self.args)
        row = json.loads(output.getvalue())
        self.assertEqual(row["setup_cost_reserve_usd"], 0.10)
        self.assertAlmostEqual(row["remaining_budget_usd"], 4.73)
        client.assert_not_called()
        self.assertFalse(self.args.output.exists())

    def test_invalid_setup_reserve_prevents_any_api_call(self):
        approval = self.replacement_approval()
        for reserve in (-0.01, 0.11, float("nan"), float("inf"), True, "0.10"):
            with self.subTest(reserve=reserve):
                self.args.approval.write_text(json.dumps({**approval, "setup_cost_reserve_usd": reserve}))
                with patch.object(create_once, "Runpod") as client:
                    with self.assertRaisesRegex(ValueError, "Setup cost reserve"):
                        create_once.create(self.args)
                    client.assert_not_called()

    def test_existing_atomic_claim_prevents_a_second_attempt(self):
        self.replacement_approval()
        self.args.approval.with_name(self.args.approval.name + ".creation-attempt").write_text("{}")
        with patch.object(create_once, "Runpod") as client:
            with self.assertRaisesRegex(ValueError, "already has a creation attempt"):
                create_once.create(self.args)
            client.assert_not_called()

    def test_claim_race_aborts_before_create(self):
        self.replacement_approval()
        client = FakeRunpod()
        def race(command, output):
            self.arm_fake(command, output)
            self.args.approval.with_name(self.args.approval.name + ".creation-attempt").write_text("{}")
        with patch.object(create_once, "Runpod", return_value=client), \
                patch.object(create_once, "detached", side_effect=race):
            with self.assertRaises(FileExistsError):
                create_once.create(self.args)
        self.assertFalse(any(call[:2] == ("pod", "create") for call in client.calls))
        self.assertTrue((self.args.output / "creation_aborted_before_submission").exists())


if __name__ == "__main__":
    unittest.main()
