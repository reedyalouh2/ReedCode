#!/usr/bin/env python3
"""Prepare a frozen task copy and launch one isolated parity client session."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import time
from urllib.parse import urlsplit
from uuid import uuid4


READY = Path(__file__).resolve().parent
PARITY = READY.parent
REPOSITORY = PARITY.parents[2]
CONTAINER_COMMAND = '''set -eu
python3 /readiness/transport_relay.py --port 18000 --host-port "$1" &
parity_relay_pid=$!
trap 'kill "$parity_relay_pid" 2>/dev/null || true' EXIT
shift
python3 /readiness/run_session.py --client codex --binary /codex \\
  --workspace /study --prompt-dir /prompts --container-isolated \\
  --upstream http://127.0.0.1:18000 --max-calls 15 "$@"
'''


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tunnel_port(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path not in ("", "/")
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.port is None or not 1 <= parsed.port <= 65535):
        raise ValueError("Use the explicit credential-free http://127.0.0.1:PORT SSH tunnel origin")
    return parsed.port


def prepare_task(workspace, source):
    workspace.mkdir(parents=True, exist_ok=False)
    task = json.loads((PARITY / "task.json").read_text())
    archive = workspace / "frozen-task.tar"
    command = ["git", "-C", str(source), "archive", "--format=tar", "--output", str(archive), task["revision"]]
    subprocess.run(command, check=True, capture_output=True, timeout=30)
    directory = workspace / "task"
    directory.mkdir()
    with tarfile.open(archive) as bundle:
        bundle.extractall(directory, filter="data")
    for name, digest in task["source_sha256"].items():
        if sha(directory / name) != digest:
            raise ValueError("Frozen task hash mismatch: " + name)
    return {"revision": task["revision"], "archive_sha256": sha(archive),
            "command": command, "source_sha256": task["source_sha256"], "kind": "git archive; no shared Git directory"}


def command_for(client, mode, workspace, binary, tunnel, seconds, docker, image, container_name, deny_read):
    tail = ["--seconds", str(seconds), "--trace-id-prefix", "parity-" + client + "-" + mode]
    if mode == "smoke":
        tail.append("--smoke")
    if client == "claude":
        command = [sys.executable, str(READY / "run_session.py"), "--client", "claude",
                   "--binary", str(binary), "--workspace", str(workspace), "--prompt-dir", str(PARITY),
                   "--upstream", tunnel, "--max-calls", "15"]
        for path in deny_read:
            command.extend(["--deny-read", str(path)])
        return command + tail
    return [docker, "run", "--rm", "--pull", "never", "--name", container_name,
            "--network", "bridge", "--platform", "linux/arm64", "--cap-add", "SYS_ADMIN",
            "--security-opt", "seccomp=" + str(READY / "container/seccomp-codex.json"),
            "--tmpfs", "/root", "--tmpfs", "/tmp", "-v", str(binary) + ":/codex:ro",
            "-v", str(READY) + ":/readiness:ro", "-v", str(PARITY) + ":/prompts:ro",
            "-v", str(workspace) + ":/study", image, "/bin/sh", "-c", CONTAINER_COMMAND,
            "parity-container", str(tunnel_port(tunnel))] + tail


def run(args):
    tunnel_port(args.tunnel)
    seconds = args.seconds if args.seconds is not None else 180 if args.mode == "smoke" else 1500
    if not 0 < seconds <= 1500:
        raise ValueError("Session seconds must be in (0, 1500]")
    if args.client == "claude" and sys.platform != "darwin":
        raise ValueError("This launcher pins the tested macOS Claude binary")
    workspace, binary = args.workspace.resolve(), args.binary.resolve()
    packet = json.loads((READY / "launch-packet.json").read_text())
    if sha(binary) != packet[args.client]["executable_sha256"]:
        raise ValueError("CLI binary hash differs from the verified version")
    provenance = json.loads((READY / "provenance.json").read_text())
    profile = json.loads((READY / "container/manifest.json").read_text())
    if sha(READY / "container/seccomp-codex.json") != profile["local_sha256"]:
        raise ValueError("Codex sandbox profile differs from the verified profile")
    task = prepare_task(workspace, args.source.resolve())
    container_name = "reedcode-parity-" + uuid4().hex
    image = provenance["container"]["image"]
    plan = {"client": args.client, "mode": args.mode, "workspace": str(workspace),
            "tunnel": args.tunnel, "seconds": seconds, "task": task,
            "cli_sha256": sha(binary), "execute": args.execute,
            "reset_requirement": "Operator clears backend cache and router index before each invocation; retain the proof.",
            "script_sha256": sha(Path(__file__)), "files": {
                name: sha(READY / name) for name in ("run_session.py", "check_clients.py", "capture_proxy.py",
                    "transport_relay.py", "launch-packet.json", "container/seccomp-codex.json")},
            "status": "prepared"}
    if args.execute and args.client == "codex":
        inspect = [args.docker, "image", "inspect", "--format", "{{.Id}}", image]
        observed = subprocess.run(inspect, capture_output=True, text=True, check=True, timeout=20).stdout.strip()
        if observed not in (provenance["container"]["image_config_digest"], provenance["container"]["image_index_digest"]):
            raise ValueError("Local Codex image differs from the verified build")
        plan["image_check"] = {"command": inspect, "id": observed}
        image = observed
    command = command_for(args.client, args.mode, workspace, binary, args.tunnel, seconds,
                          args.docker, image, container_name, args.deny_read)
    plan["command"] = command
    plan_path = workspace / "launch.json"
    plan_path.write_text(json.dumps(plan, indent=2) + "\n")
    if not args.execute:
        print(json.dumps({"launch": str(plan_path), "execute": False, "model_requests_made": 0}, indent=2))
        return 0
    plan.update(status="running", started_unix=time.time())
    plan_path.write_text(json.dumps(plan, indent=2) + "\n")
    try:
        with (workspace / "launcher.stdout").open("wb") as stdout, (workspace / "launcher.stderr").open("wb") as stderr:
            result = subprocess.run(command, stdout=stdout, stderr=stderr, timeout=seconds + 90)
        plan.update(status="completed" if result.returncode == 0 else "client_failed", exit_code=result.returncode)
        return result.returncode
    except BaseException as error:
        plan.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        if args.client == "codex":
            cleanup = subprocess.run([args.docker, "rm", "-f", container_name], capture_output=True, timeout=20)
            plan["container_cleanup"] = {"exit_code": cleanup.returncode,
                                         "stderr": cleanup.stderr.decode(errors="replace")}
        plan["ended_unix"] = time.time()
        plan_path.write_text(json.dumps(plan, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=("claude", "codex"), required=True)
    parser.add_argument("--mode", choices=("smoke", "full"), required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--tunnel", default="http://127.0.0.1:18002")
    parser.add_argument("--source", type=Path, default=REPOSITORY)
    parser.add_argument("--docker", default="/usr/local/bin/docker")
    parser.add_argument("--seconds", type=float)
    parser.add_argument("--deny-read", type=Path, action="append", default=[])
    parser.add_argument("--execute", action="store_true")
    raise SystemExit(run(parser.parse_args()))
