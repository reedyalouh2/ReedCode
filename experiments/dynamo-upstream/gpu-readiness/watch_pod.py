"""Local teardown guard. It cannot enforce a deadline if this host is offline."""

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import time


def action(elapsed, *, ready, finished, rate, disk_gb):
    if not all(math.isfinite(v) for v in (elapsed, rate, disk_gb)):
        return "delete", "invalid_cost_inputs"
    if rate <= 0 or rate > 1.65 or disk_gb < 0 or disk_gb > 150:
        return "delete", "quote_outside_plan"
    if finished:
        return "delete", "study_finished"
    if elapsed >= 35 * 60 and not ready:
        return "delete", "readiness_deadline"
    if elapsed >= 140 * 60:
        return "delete", "teardown_deadline"
    estimated_cost = max(elapsed, 0) / 3600 * (rate + disk_gb * 0.10 / 720)
    if estimated_cost >= 4.20:
        return "delete", "cost_margin_reached"
    if elapsed >= 130 * 60:
        return "stop_requests", "request_deadline"
    return "wait", "within_budget"


def private_key(path):
    path = Path(path)
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Key file must be owned by this user and readable only by them")
    key = path.read_text().strip()
    if not key.startswith("rpa_") or len(key) < 20 or any(c.isspace() for c in key):
        raise ValueError("Expected a plain Runpod key file")
    return key


class Runpod:
    def __init__(self, executable, key_file):
        self.executable = str(executable)
        self.key = private_key(key_file)

    def command(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith("RUNPOD_")}
        env["RUNPOD_API_KEY"] = self.key
        try:
            result = subprocess.run([self.executable, *args], env=env,
                                    capture_output=True, text=True, timeout=25)
        except (OSError, subprocess.TimeoutExpired):
            return None, "local_command_failure"
        if result.returncode:
            for line in result.stderr.splitlines():
                try:
                    error = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if error.get("code") in {"not_found", "network_error", "rate_limited",
                                         "server_error", "unauthorized", "forbidden"}:
                    return None, error["code"]
            return None, "unclassified_cli_failure"
        try:
            return json.loads(result.stdout), None
        except json.JSONDecodeError:
            return None, "invalid_cli_json"


def delete_and_verify(client, pod_id):
    _, error = client.command("pod", "delete", pod_id)
    if error and error != "not_found":
        return False, error
    pods, error = client.command("pod", "list")
    if error:
        return False, error
    if not isinstance(pods, list) or any(not isinstance(p, dict) or "id" not in p for p in pods):
        return False, "invalid_pod_list"
    if any(p["id"] == pod_id for p in pods):
        return False, "deletion_pending"
    return True, "absent_from_pod_list"


def monitor(state, directory, client, *, wall=time.time, monotonic=time.monotonic,
            sleep=time.sleep):
    if state.get("local_guard_approved") is not True:
        raise ValueError("The provider timer is unavailable; approval of the local substitute is required")
    pod_id = state["pod_id"]
    if not isinstance(pod_id, str) or not pod_id.isalnum():
        raise ValueError("Invalid pod ID")
    started = state["allocation_started_unix"]
    if not isinstance(started, (int, float)) or not math.isfinite(started) or started > wall():
        raise ValueError("Allocation start must be recorded before creation")
    elapsed_on_start = max(0, wall() - started)
    monotonic_start = monotonic()
    deleting = False
    consecutive_errors = 0
    log_path = directory / "watchdog.jsonl"

    def log(event, **fields):
        with log_path.open("a") as out:
            out.write(json.dumps({"utc": datetime.now(timezone.utc).isoformat(),
                                  "event": event, "pod_id": pod_id, **fields}) + "\n")

    (directory / "watchdog_started").write_text(str(os.getpid()) + "\n")
    while True:
        heartbeat = directory / "watchdog-heartbeat.json"
        temporary = heartbeat.with_suffix(".tmp")
        temporary.write_text(json.dumps({"pod_id": pod_id, "pid": os.getpid(), "at_unix": wall()}))
        temporary.replace(heartbeat)
        elapsed = max(wall() - started, elapsed_on_start + monotonic() - monotonic_start)
        choice, reason = action(elapsed, ready=(directory / "ready").exists(),
                                finished=(directory / "finished").exists(),
                                rate=state["compute_rate_per_hour"], disk_gb=state["disk_gb"])
        if choice in {"stop_requests", "delete"}:
            (directory / "stop_requests").touch()
        deleting |= choice == "delete"
        if deleting:
            success, status = delete_and_verify(client, pod_id)
            log("delete_check", elapsed_s=elapsed, reason=reason, status=status)
            if success:
                (directory / "deleted").write_text(status + "\n")
                return
        else:
            pod, error = client.command("pod", "get", pod_id)
            if error == "not_found":
                (directory / "deleted").write_text("not_found\n")
                log("already_deleted", elapsed_s=elapsed)
                return
            if error:
                consecutive_errors += 1
                log("read_error", status=error, elapsed_s=elapsed)
                deleting = consecutive_errors >= 2
            else:
                consecutive_errors = 0
                if not isinstance(pod, dict) or pod.get("id") != pod_id or pod.get("name") != state["pod_name"]:
                    deleting = True
                    log("identity_response_mismatch", elapsed_s=elapsed)
                else:
                    actual_rate = pod.get("costPerHr")
                    if not isinstance(actual_rate, (int, float)) or not math.isfinite(actual_rate) or actual_rate <= 0 or actual_rate > 1.65:
                        deleting = True
                        log("quote_changed_or_unknown", elapsed_s=elapsed)
        # Retry deletion until absence is confirmed, including after transient API failures.
        sleep(10)


def reconcile(intent, directory, client, *, wall=time.time, sleep=time.sleep):
    """Wait for this allocation name, then hand the same process to its guard."""
    from study_cleanup import cleanup, identify_created

    if intent.get("local_guard_approved") is not True:
        raise ValueError("Local guard approval is required")
    name = intent["pod_name"]
    if not isinstance(name, str) or not name.startswith("reedcode-combined-"):
        raise ValueError("Expected the unique study allocation name")
    with (directory / "reconciliation.jsonl").open("a") as log:
        while True:
            heartbeat = directory / "reconciliation-heartbeat.json"
            temporary = heartbeat.with_suffix(".tmp")
            temporary.write_text(json.dumps({"pod_name": name, "pid": os.getpid(), "at_unix": wall()}))
            temporary.replace(heartbeat)
            if (directory / "creation_aborted_before_submission").exists():
                return
            try:
                pod_ids = identify_created(client, name)
                status = "found" if pod_ids else "not_yet_visible"
            except RuntimeError:
                pod_ids, status = [], "reconciliation_unavailable"
            log.write(json.dumps({"at_unix": wall(), "status": status, "pod_ids": pod_ids}) + "\n")
            log.flush()
            if len(pod_ids) == 1:
                state = {**intent, "pod_id": pod_ids[0]}
                (directory / "reconciled-state.json").write_text(json.dumps(state, indent=2) + "\n")
                monitor(state, directory, client)
                return
            if pod_ids:
                (directory / "stop_requests").touch()
                cleanup(client, pod_ids, directory, sleep=sleep)
                return
            # API recovery can take longer than the readiness window.
            sleep(10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--state", type=Path)
    identity.add_argument("--intent", type=Path)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--cli", type=Path, required=True)
    args = parser.parse_args()
    path = args.intent or args.state
    state = json.loads(path.read_text())
    client = Runpod(args.cli, args.key_file)
    if args.intent:
        reconcile(state, path.parent, client)
    else:
        monitor(state, path.parent, client)


if __name__ == "__main__":
    main()
