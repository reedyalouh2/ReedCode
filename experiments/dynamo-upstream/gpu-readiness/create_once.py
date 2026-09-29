"""Arm cleanup, create the approved study pod once, and track its identity."""

import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import uuid

from study_cleanup import cleanup
from watch_pod import Runpod


IMAGE = "nvcr.io/nvidia/ai-dynamo/vllm-runtime@sha256:d18389c89eb319401fdd73f1fbbaff10d9382f634b879941dfba75bbf7260c1e"
GPU = "NVIDIA A100-SXM4-80GB"
HERE = Path(__file__).resolve().parent
GATES = {"linux_match", "installed_warmup", "real_clients", "passive_capture",
         "kv_collector", "reset_replay", "launch_packet"}


def validate_gates(path, approval_path=None):
    data = json.loads(path.read_text())
    if set(data["gates"]) != GATES or any(value is not True for value in data["gates"].values()):
        raise ValueError("Every pre-rental gate must be verified")
    if not data.get("artifacts"):
        raise ValueError("Readiness artifacts must be frozen")
    for item in data["artifacts"]:
        if hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("Readiness artifact changed: " + item["path"])
    approval_path = approval_path or HERE / "approval.json"
    approval = json.loads(approval_path.read_text())
    for key in ("approved", "local_guard_approved", "previous_key_revoked_user_confirmed"):
        if approval.get(key) is not True:
            raise ValueError("Required approval is absent")
    if (approval["total_cost_cap_usd"] != 5 or approval["resources_created"] != 0
            or approval.get("creation_attempted") or approval.get("maximum_rentals", 1) != 1):
        raise ValueError("The one-rental approval is unavailable")
    image = approval.get("approved_image", IMAGE)
    if not isinstance(image, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}", image):
        raise ValueError("The approved image must use a pinned SHA-256 digest")
    prior = approval.get("prior_cost_reserve_usd", 0)
    if type(prior) not in (int, float) or not math.isfinite(prior) or not 0 <= prior <= 0.17:
        raise ValueError("Prior cost reserve must preserve the $4.20 guard within the $5 total cap")
    setup = approval.get("setup_cost_reserve_usd", 0)
    if type(setup) not in (int, float) or not math.isfinite(setup) or not 0 <= setup <= 0.10:
        raise ValueError("Setup cost reserve must be between $0 and $0.10")
    if approval["total_cost_cap_usd"] - prior - setup < 4.20:
        raise ValueError("Combined reserves must preserve the $4.20 guard within the $5 total cap")
    if approval_path.with_name(approval_path.name + ".creation-attempt").exists():
        raise ValueError("The one-rental approval already has a creation attempt")
    return approval


def detached(command, output):
    with output.open("ab") as log:
        return subprocess.Popen(["/usr/bin/caffeinate", "-i", "-s", *command],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                start_new_session=True, close_fds=True)


def create(args):
    approval_path = (getattr(args, "approval", None) or HERE / "approval.json").resolve()
    approval = validate_gates(args.gates, approval_path)
    approval_sha = hashlib.sha256(approval_path.read_bytes()).hexdigest()
    image = approval.get("approved_image", IMAGE)
    prior_cost = approval.get("prior_cost_reserve_usd", 0)
    setup_cost = approval.get("setup_cost_reserve_usd", 0)
    public_key = args.ssh_public_key.read_text().strip()
    if not public_key.startswith("ssh-ed25519 ") or "\n" in public_key:
        raise ValueError("Expected the dedicated study SSH public key")
    if not args.execute:
        print(json.dumps({"execute": False, "image": image, "gpu": GPU, "disk_gb": 150,
                          "maximum_compute_rate": 1.65, "gates": "verified",
                          "approval": str(approval_path), "prior_cost_reserve_usd": prior_cost,
                          "setup_cost_reserve_usd": setup_cost,
                          "remaining_budget_usd": 5 - prior_cost - setup_cost}))
        return
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    client = Runpod(args.cli, args.key_file)
    pods, error = client.command("pod", "list")
    if error or pods != []:
        raise ValueError("Expected an empty pod list before this one-pod study")
    account, error = client.command("user")
    if error or not isinstance(account, dict):
        raise ValueError("Account balance could not be checked")
    balance, spending = account.get("clientBalance"), account.get("currentSpendPerHr")
    if type(balance) not in (int, float) or balance < 5 or spending != 0:
        raise ValueError("Expected at least $5 balance and zero existing active spend")
    (args.output / "billing-before.json").write_text(json.dumps({
        "balance": balance, "currentSpendPerHr": spending, "checked_unix": time.time()}, indent=2) + "\n")
    quotes, error = client.command("gpu", "list")
    if error:
        raise ValueError("GPU quote unavailable: " + error)
    quote = next(row for row in quotes if row.get("gpuId") == GPU)
    rate = quote.get("securePricePerHr")
    if not quote.get("available") or not quote.get("secureCloud") or type(rate) not in (int, float) or not 0 < rate <= 1.65:
        raise ValueError("Approved GPU is unavailable within the quote ceiling")
    (args.output / "quote.json").write_text(json.dumps(quote, indent=2) + "\n")
    name = "reedcode-combined-" + uuid.uuid4().hex[:12]
    boot = base64.b64encode((HERE / "bootstrap_ssh.py").read_bytes()).decode()
    boot_command = " && ".join([
        "python3 -m venv --system-site-packages /tmp/reedcode-control-venv",
        "/tmp/reedcode-control-venv/bin/python -m pip install --disable-pip-version-check asyncssh==2.23.0",
        shlex.join(["/tmp/reedcode-control-venv/bin/python", "-u", "-c",
                    'import base64,os; exec(base64.b64decode(os.environ["REEDCODE_BOOTSTRAP"]))'])])
    command = ["pod", "create", "--name", name, "--image", image, "--gpu-id", GPU,
               "--gpu-count", "1", "--cloud-type", "SECURE", "--container-disk-in-gb", "150",
               "--volume-in-gb", "0", "--ports", "22/tcp", "--min-cuda-version", "13.0",
               "--ssh=false", "--env", json.dumps({"PUBLIC_KEY": public_key, "REEDCODE_BOOTSTRAP": boot}),
               "--docker-args", json.dumps({"entrypoint": ["/bin/bash"], "cmd": ["-lc", boot_command]})]
    started = time.time()
    intent = {"pod_name": name, "allocation_started_unix": started, "image": image,
              "compute_rate_per_hour": rate, "disk_gb": 150, "local_guard_approved": True,
              "approval_path": str(approval_path), "approval_sha256_before_attempt": approval_sha,
              "total_cost_cap_usd": 5, "prior_cost_reserve_usd": prior_cost,
              "setup_cost_reserve_usd": setup_cost,
              "remaining_budget_usd": 5 - prior_cost - setup_cost,
              "creation_attempts": 1, "gates_sha256": hashlib.sha256(args.gates.read_bytes()).hexdigest()}
    (args.output / "creation-intent.json").write_text(json.dumps(intent, indent=2) + "\n")
    (args.output / "creation-command.json").write_text(json.dumps(command, indent=2) + "\n")
    guard_args = ["--intent", str(args.output / "creation-intent.json"),
                  "--key-file", str(args.key_file), "--cli", str(args.cli)]
    try:
        detached([sys.executable, str(HERE / "watch_pod.py"), *guard_args],
                 args.output / "watchdog-process.log")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (args.output / "reconciliation-heartbeat.json").exists():
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Pre-allocation reconciliation guard did not become ready")
        if hashlib.sha256(approval_path.read_bytes()).hexdigest() != approval_sha:
            raise ValueError("Approval changed before the creation attempt")
        attempt_path = approval_path.with_name(approval_path.name + ".creation-attempt")
        with attempt_path.open("x") as claimed:
            claimed.write(json.dumps({"output": str(args.output), "claimed_unix": time.time()}) + "\n")
        approval = json.loads(approval_path.read_text())
        approval["creation_attempted"] = True
        approval_path.write_text(json.dumps(approval, indent=2) + "\n")
    except BaseException:
        (args.output / "creation_aborted_before_submission").touch()
        raise
    (args.output / "creation_submitted").touch()
    try:
        pod, error = client.command(*command)
    except BaseException:
        (args.output / "stop_requests").touch()
        (args.output / "finished").touch()
        raise
    pod_ids = []
    if error or not isinstance(pod, dict) or not pod.get("id"):
        (args.output / "stop_requests").touch()
        (args.output / "finished").touch()
        (args.output / "uncertain-creation.json").write_text(json.dumps({
            "at_unix": time.time(), "status": "detached_exact_name_cleanup_pending"}) + "\n")
        raise RuntimeError("Uncertain creation; detached guard keeps reconciling and cleaning up. No study run")
    pod_ids = [pod["id"]]
    try:
        if pod.get("name") != name or not isinstance(pod["id"], str) or not pod["id"].isalnum():
            raise ValueError("Created pod identity differs from intent")
        expected = {"imageName": image, "containerDiskInGb": 150, "volumeInGb": 0, "gpuCount": 1}
        if any(pod.get(key) != value for key, value in expected.items()):
            raise ValueError("Created pod resources differ from the approved request")
        actual_rate = pod.get("costPerHr")
        if type(actual_rate) not in (int, float) or not 0 < actual_rate <= 1.65:
            raise ValueError("Created pod quote is outside the budget")
        state = {**intent, "pod_id": pod["id"], "compute_rate_per_hour": actual_rate}
        state_path = args.output / "state.json"
        state_path.write_text(json.dumps(state, indent=2) + "\n")
        shared = ["--state", str(state_path), "--key-file", str(args.key_file), "--cli", str(args.cli)]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if (args.output / "watchdog-heartbeat.json").exists():
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Detached guard did not become ready")
        controller_command = args.output / "controller-command.json"
        controller_command.write_text(json.dumps([sys.executable, str(HERE / "hold_controller.py"),
                                                   "--state", str(state_path)]))
        detached([sys.executable, str(HERE / "study_cleanup.py"), *shared,
                  "--command-file", str(controller_command)], args.output / "controller-process.log")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if (args.output / "controller-heartbeat.json").exists():
                break
            if (args.output / "deleted").exists() or (args.output / "controller_deleted").exists():
                raise RuntimeError("Cleanup ran before the controller became ready")
            time.sleep(0.1)
        else:
            raise RuntimeError("Independent cleanup controller did not become ready")
        approval = json.loads(approval_path.read_text())
        approval["resources_created"] = 1
        approval_path.write_text(json.dumps(approval, indent=2) + "\n")
        print(json.dumps({"pod_id": pod["id"], "state": str(state_path),
                          "compute_rate_per_hour": actual_rate, "allocation_started_unix": started}))
    except BaseException:
        cleanup(client, pod_ids, args.output)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gates", type=Path, required=True)
    parser.add_argument("--approval", type=Path, help="Distinct local approval for this one creation attempt; defaults to approval.json")
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--ssh-public-key", type=Path, required=True)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    create(parser.parse_args())
