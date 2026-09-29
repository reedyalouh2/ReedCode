"""Use the dedicated SSH key for the single recorded study pod."""

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shlex
import subprocess

from watch_pod import Runpod


REMOTE_FINGERPRINT = '''import hashlib,json,os,sys
with open(sys.argv[1], "rb") as source:
    before = os.fstat(source.fileno())
    digest = hashlib.sha256()
    size = 0
    while block := source.read(1024*1024):
        size += len(block)
        digest.update(block)
    after = os.fstat(source.fileno())
    assert (before.st_size,before.st_mtime_ns,before.st_ino) == (after.st_size,after.st_mtime_ns,after.st_ino)
    assert size == after.st_size
print(json.dumps({"size": size, "sha256": digest.hexdigest()}))
'''


def fingerprint(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        before = os.fstat(source.fileno())
        while block := source.read(1024 * 1024):
            size += len(block)
            digest.update(block)
        after = os.fstat(source.fileno())
    if (size != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ino)
            != (after.st_size, after.st_mtime_ns, after.st_ino)):
        raise ValueError("Local transfer file changed while hashing")
    return {"size": size, "sha256": digest.hexdigest()}


def transfer(operation, local, remote_path, target, port, options, run=subprocess.run):
    if not remote_path.startswith("/tmp/reedcode-"):
        raise ValueError("Remote transfers must stay under a study directory")

    def remote_fingerprint():
        command = ["ssh", *options, "-p", str(port), target,
                   shlex.join(["python3", "-c", REMOTE_FINGERPRINT, remote_path])]
        result = run(command, capture_output=True, timeout=45)
        if result.returncode != 0:
            raise ValueError("Remote transfer file could not be verified")
        value = json.loads(result.stdout)
        if (not isinstance(value, dict) or set(value) != {"size", "sha256"}
                or type(value["size"]) is not int or value["size"] < 0
                or not isinstance(value["sha256"], str) or len(value["sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in value["sha256"])):
            raise ValueError("Invalid remote file fingerprint")
        return value

    expected = fingerprint(local) if operation == "upload" else remote_fingerprint()
    remote = target + ":" + remote_path
    endpoints = [str(local), remote] if operation == "upload" else [remote, str(local)]
    result = run(["scp", *options, "-P", str(port), *endpoints])
    if result.returncode not in (0, 1):
        raise ValueError(f"SCP failed with exit status {result.returncode}")
    actual_local, actual_remote = fingerprint(local), remote_fingerprint()
    if expected != actual_local or expected != actual_remote:
        raise ValueError("Transfer verification failed: complete file hashes or sizes differ")
    # AsyncSSH SFTP can finish a copy without sending an SSH channel exit status.
    print(json.dumps({"transfer_verified": True, "operation": operation,
                      "scp_exit_code": result.returncode, **expected}), flush=True)
    return 0


def connection(state, cli, key_file):
    client = Runpod(cli, key_file)
    pod, error = client.command("pod", "get", state["pod_id"])
    if error or not isinstance(pod, dict) or pod.get("name") != state["pod_name"]:
        raise ValueError("Recorded pod is unavailable")
    info, error = client.command("ssh", "info", state["pod_id"])
    if error or not isinstance(info, dict) or info.get("id") != state["pod_id"] or info.get("name") != state["pod_name"]:
        raise ValueError("Recorded pod SSH connection is unavailable")
    address = str(ipaddress.ip_address(info["ip"]))
    port = int(info["port"])
    if not 0 < port < 65536:
        raise ValueError("Invalid SSH port")
    return address, port


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--ssh-key", type=Path, required=True)
    sub = parser.add_subparsers(dest="operation", required=True)
    sub.add_parser("check")
    execute = sub.add_parser("exec")
    execute.add_argument("argv", nargs="+")
    upload = sub.add_parser("upload")
    upload.add_argument("local", type=Path)
    upload.add_argument("remote")
    download = sub.add_parser("download")
    download.add_argument("remote")
    download.add_argument("local", type=Path)
    sub.add_parser("tunnel")
    args = parser.parse_args()
    state = json.loads(args.state.read_text())
    host, port = connection(state, args.cli, args.key_file)
    options = ["-i", str(args.ssh_key), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
               "-o", "StrictHostKeyChecking=accept-new", "-o",
               "UserKnownHostsFile=" + str(args.state.parent / "known_hosts"),
               "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15",
               "-o", "ServerAliveCountMax=3"]
    target = "root@" + host
    if args.operation == "check":
        command = ["ssh", *options, "-p", str(port), target,
                   "python3 -c 'import platform; print(platform.system(), platform.machine())'"]
    elif args.operation == "exec":
        command = ["ssh", *options, "-p", str(port), target, shlex.join(args.argv)]
    elif args.operation == "tunnel":
        command = ["ssh", *options, "-p", str(port), "-o", "ExitOnForwardFailure=yes", "-N",
                   "-L", "127.0.0.1:18002:127.0.0.1:8000",
                   "-L", "127.0.0.1:18081:127.0.0.1:8081",
                   "-L", "127.0.0.1:18082:127.0.0.1:8082",
                   "-L", "127.0.0.1:19099:127.0.0.1:9099", target]
    else:
        return transfer(args.operation, args.local, args.remote, target, port, options)
    return subprocess.run(command).returncode


if __name__ == "__main__":
    raise SystemExit(main())
