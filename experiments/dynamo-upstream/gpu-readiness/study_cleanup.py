"""Keep teardown in the controller's exit path as well as the detached guard."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import time

from watch_pod import Runpod, delete_and_verify


def identify_created(client, name):
    pods, error = client.command("pod", "list")
    if error or not isinstance(pods, list) or any(not isinstance(pod, dict) for pod in pods):
        raise RuntimeError("Cannot reconcile allocation: " + (error or "invalid_pod_list"))
    matching = [p for p in pods if p.get("name") == name]
    if any(not isinstance(p.get("id"), str) or not p["id"].isalnum() for p in matching):
        raise RuntimeError("Invalid matching pod identity")
    return [p["id"] for p in matching]


def cleanup(client, pod_ids, directory, *, sleep=time.sleep):
    pending = set(pod_ids)
    with (directory / "controller-cleanup.jsonl").open("a") as log:
        while pending:
            for pod_id in sorted(pending):
                done, status = delete_and_verify(client, pod_id)
                log.write(json.dumps({"at_unix": time.time(), "pod_id": pod_id,
                                      "deleted": done, "status": status}) + "\n")
                log.flush()
                if done:
                    pending.remove(pod_id)
            if pending:
                sleep(10)
    (directory / "controller_deleted").write_text("All recorded study pods absent\n")


def run_study(client, state, directory, command, *, run=subprocess.run, sleep=time.sleep):
    if state.get("local_guard_approved") is not True:
        raise ValueError("Changed teardown control has not been approved")
    if not command or not all(isinstance(arg, str) and arg for arg in command):
        raise ValueError("Study command must be a nonempty argv list")
    pod_id = state["pod_id"]
    seconds_left = state["allocation_started_unix"] + 140 * 60 - time.time()
    try:
        pod, error = client.command("pod", "get", pod_id)
        if error or not isinstance(pod, dict) or pod.get("id") != pod_id or pod.get("name") != state["pod_name"]:
            raise ValueError("Study pod does not match the recorded allocation")
        heartbeat = json.loads((directory / "watchdog-heartbeat.json").read_text())
        if heartbeat["pod_id"] != pod_id or not 0 <= time.time() - heartbeat["at_unix"] <= 45:
            raise ValueError("Detached watchdog heartbeat is stale or mismatched")
        pid = heartbeat["pid"]
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise ValueError("Detached watchdog PID is invalid")
        os.kill(pid, 0)
        if seconds_left <= 0:
            raise TimeoutError("Rental teardown deadline has already arrived")
        return run(command, timeout=seconds_left).returncode
    finally:
        (directory / "stop_requests").touch()
        (directory / "finished").touch()
        cleanup(client, [pod_id], directory, sleep=sleep)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--command-file", type=Path, required=True,
                        help="JSON argv list; commands run without a shell")
    args = parser.parse_args()
    state = json.loads(args.state.read_text())
    client = Runpod(args.cli, args.key_file)
    return run_study(client, state, args.state.parent,
                     json.loads(args.command_file.read_text()))


if __name__ == "__main__":
    raise SystemExit(main())
