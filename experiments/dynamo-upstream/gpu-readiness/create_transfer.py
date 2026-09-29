"""Create one guarded CPU pod for the fixed public registry transfer."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from create_once import detached
from study_cleanup import cleanup
from watch_pod import Runpod, private_key


HERE = Path(__file__).resolve().parent
IMAGE = "gcr.io/go-containerregistry/crane@sha256:18efc5092649b2a674d384663044e04edf76705c4faa2875e8a322b4e80f070a"
SOURCE = "nvcr.io/nvidia/ai-dynamo/vllm-runtime@sha256:d18389c89eb319401fdd73f1fbbaff10d9382f634b879941dfba75bbf7260c1e"
DESTINATION = "ttl.sh/reedcode-dynamo-92cfdea446c94719b36df5db02cc8189:24h"
COPY_ARGS = ["copy", "--jobs", "8", "--platform", "linux/amd64", SOURCE, DESTINATION]
CATALOG = "/v2/catalog/cpus?include=AVAILABILITY&product=POD&vcpuCount=2"


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def api(key, method, path, body=None):
    request = Request("https://api.runpod.io" + path, method=method,
        data=None if body is None else json.dumps(body).encode(), headers={
            "Authorization": "Bearer " + key, "User-Agent": "runpodctl/2.10.0",
            "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urlopen(request, timeout=25) as response:
            return json.load(response)
    except HTTPError as error:
        raise RuntimeError("Runpod HTTP status " + str(error.code)) from None
    except (URLError, TimeoutError):
        raise RuntimeError("Runpod transport failure") from None


def body_for(name):
    return {"name": name, "image": IMAGE, "cloud": "SECURE", "cpu": {"id": "cpu3c", "vcpuCount": 2},
            "disk": 10, "ports": [], "env": {}, "mounts": {}, "startSsh": False,
            "startJupyter": False, "entrypoint": ["/ko-app/crane"], "cmd": COPY_ARGS}


def quote_rate(catalog):
    rows = [row for row in catalog["cpus"] if row.get("id") == "cpu3c"]
    if len(rows) != 1:
        raise ValueError("Expected one cpu3c catalog entry")
    row = rows[0]
    rate = row["price"]["securePerVcpu"] * 2
    if (row.get("availability") not in ("HIGH", "MEDIUM", "LOW")
            or not row["vcpu"]["min"] <= 2 <= row["vcpu"]["max"]
            or row["ramGbPerVcpu"] != 2 or not math.isfinite(rate) or not 0 < rate <= 0.10):
        raise ValueError("Two-vCPU transfer pod is unavailable within its price ceiling")
    return rate


def validate_created(pod, name):
    expected = body_for(name)
    for key in ("name", "image", "cloud", "disk", "ports", "env"):
        if pod.get(key) != expected[key]:
            raise ValueError("Created transfer resource differs: " + key)
    if pod.get("gpu") or pod.get("cpu") != {"id": "cpu3c", "vcpuCount": 2, "memory": 4} or pod.get("mounts") not in ({}, None):
        raise ValueError("Created transfer pod is not the approved CPU-only shape")
    entrypoint, command = pod.get("entrypoint"), pod.get("cmd")
    if entrypoint is None or command is None:
        encoded = json.loads(pod.get("args", "{}"))
        entrypoint, command = encoded.get("entrypoint"), encoded.get("cmd")
    if entrypoint != expected["entrypoint"] or command != expected["cmd"]:
        raise ValueError("Created transfer command differs")
    rate = pod.get("cost")
    if type(rate) not in (int, float) or not math.isfinite(rate) or not 0 < rate <= 0.10:
        raise ValueError("Created transfer quote exceeds $0.10/hour")
    return rate


def create(args):
    approval_path = args.approval.resolve()
    approval_raw = approval_path.read_bytes()
    approval = json.loads(approval_raw)
    required = ("approved", "cpu_transfer_approved", "local_guard_approved")
    if (any(approval.get(key) is not True for key in required) or approval.get("total_cost_cap_usd") != 5
            or approval.get("helper_cost_cap_usd") != 0.10 or approval.get("resources_created") != 0
            or approval.get("creation_attempted")):
        raise ValueError("A separate unused CPU-transfer approval is required")
    claim = approval_path.with_name(approval_path.name + ".creation-attempt")
    if claim.exists():
        raise ValueError("This CPU transfer approval already has an attempt")
    name = "reedcode-combined-transfer-" + uuid4().hex[:12]
    body = body_for(name)
    if not args.execute:
        print(json.dumps({"execute": False, "request": body, "helper_cost_cap_usd": 0.10,
                          "guard_deadline_minutes": 35, "remote_credentials": False}, indent=2))
        return
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    client = Runpod(args.cli, args.key_file)
    key = private_key(args.key_file)
    pods, error = client.command("pod", "list")
    if error or pods != []:
        raise ValueError("Expected no existing pods before the transfer")
    account, error = client.command("user")
    if (error or not isinstance(account, dict) or type(account.get("clientBalance")) not in (int, float)
            or account["clientBalance"] < 5 or account.get("currentSpendPerHr") != 0):
        raise ValueError("Expected at least $5 account balance and no active spend")
    catalog = api(key, "GET", CATALOG)
    rate = quote_rate(catalog)
    save(args.output / "catalog.json", catalog)
    save(args.output / "billing-before.json", {"balance": account["clientBalance"], "currentSpendPerHr": 0})
    intent = {"pod_name": name, "allocation_started_unix": time.time(), "image": IMAGE,
              "compute_rate_per_hour": rate, "disk_gb": 10, "local_guard_approved": True,
              "creation_attempts": 1, "helper_cost_cap_usd": 0.10,
              "approval_path": str(approval_path), "approval_sha256": hashlib.sha256(approval_raw).hexdigest(),
              "purpose": "Copy public pinned image layers only", "never_mark_ready": True,
              "estimated_35_minute_cost_at_ceiling": (0.10 + 10 * 0.10 / 720) * 35 / 60}
    save(args.output / "creation-intent.json", intent)
    save(args.output / "creation-request.json", body)
    try:
        detached([sys.executable, str(HERE / "watch_pod.py"), "--intent", str(args.output / "creation-intent.json"),
                  "--key-file", str(args.key_file), "--cli", str(args.cli)], args.output / "watchdog-process.log")
        deadline = time.monotonic() + 10
        while not (args.output / "reconciliation-heartbeat.json").exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("Transfer guard did not become ready")
            time.sleep(0.1)
        if approval_path.read_bytes() != approval_raw:
            raise ValueError("Transfer approval changed before submission")
        with claim.open("x") as claimed:
            claimed.write(str(args.output) + "\n")
        approval["creation_attempted"] = True
        save(approval_path, approval)
    except BaseException:
        (args.output / "creation_aborted_before_submission").touch()
        raise
    (args.output / "creation_submitted").touch()
    try:
        pod = api(key, "POST", "/v2/pods", body)
    except BaseException:
        (args.output / "finished").touch()
        (args.output / "stop_requests").touch()
        save(args.output / "uncertain-creation.json", {"status": "exact-name reconciliation remains armed; never retry create"})
        raise
    pod_id = pod.get("id") if isinstance(pod, dict) else None
    if not isinstance(pod_id, str) or not pod_id.isalnum() or pod.get("name") != name:
        (args.output / "finished").touch()
        (args.output / "stop_requests").touch()
        raise RuntimeError("Invalid create response; detached guard keeps reconciling the exact name")
    try:
        actual_rate = validate_created(pod, name)
        public = {key: pod.get(key) for key in ("id", "name", "image", "cloud", "cpu", "gpu", "disk", "mounts",
                  "ports", "entrypoint", "cmd", "args", "cost", "status", "createdAt", "dataCenterId")}
        save(args.output / "created.json", public)
        state = {**intent, "pod_id": pod_id, "compute_rate_per_hour": actual_rate}
        save(args.output / "state.json", state)
        deadline = time.monotonic() + 20
        while not (args.output / "watchdog-heartbeat.json").exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("Transfer guard did not adopt the allocated pod")
            time.sleep(0.1)
        approval["resources_created"] = 1
        save(approval_path, approval)
        print(json.dumps({"pod_id": pod_id, "state": str(args.output / "state.json"),
                          "compute_rate_per_hour": actual_rate, "helper_cost_cap_usd": 0.10}))
    except BaseException:
        (args.output / "finished").touch()
        (args.output / "stop_requests").touch()
        cleanup(client, [pod_id], args.output)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("approval", "key-file", "cli", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    create(parser.parse_args())
