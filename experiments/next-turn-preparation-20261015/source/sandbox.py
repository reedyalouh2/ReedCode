"""Run model tools in one restricted Docker container per task."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent
FILE_TOOL = (ROOT / "file_tool.py").read_text()


def creation_command(task, workspace_root, name):
    workspace = Path(task["workspace"]).resolve(strict=True)
    root = Path(workspace_root).resolve(strict=True)
    if (
        workspace == root
        or not workspace.is_relative_to(root)
        or not workspace.is_dir()
    ):
        raise ValueError(
            "workspace must be a dedicated directory inside workspace_root"
        )
    if "," in str(workspace):
        raise ValueError("Docker mount paths cannot contain commas")
    image = task["container_image"]
    if not re.fullmatch(
        r"(?:sha256:[0-9a-f]{64}|[A-Za-z0-9._/:~-]+@sha256:[0-9a-f]{64})", image
    ):
        raise ValueError("container_image must be an immutable image ID or digest")
    platform = task.get("container_platform", "linux/amd64")
    if platform not in ("linux/amd64", "linux/arm64"):
        raise ValueError("unsupported container platform")
    return [
        "docker",
        "create",
        "--name",
        name,
        "--platform",
        platform,
        "--label",
        "reedcode.live-capture=true",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--pids-limit",
        "256",
        "--memory",
        "2g",
        "--cpus",
        "1",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--workdir",
        "/workspace",
        "--env",
        "HOME=/tmp/home",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        "PYTHONUNBUFFERED=1",
        "--env",
        "GIT_CONFIG_GLOBAL=/dev/null",
        "--env",
        "GIT_CONFIG_NOSYSTEM=1",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=536870912",
        "--mount",
        f"type=bind,src={workspace},dst=/workspace",
        "--entrypoint",
        "/bin/sh",
        image,
        "-c",
        "mkdir -p /tmp/home && exec sleep infinity",
    ]


class DockerSandbox:
    def __init__(self, task, workspace_root):
        self.task = task
        self.name = "reedcode-live-" + uuid.uuid4().hex
        self.command = creation_command(task, workspace_root, self.name)
        self.container = None
        self.usable = True

    def start(self):
        # Images and dependencies are prepared before the model run.
        commit = subprocess.run(
            ["git", "-C", self.task["workspace"], "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        ).stdout.strip()
        expected = self.task.get("workspace_snapshot_commit", self.task["base_commit"])
        if commit != expected:
            raise ValueError("scratch workspace does not match its pinned snapshot")
        subprocess.run(
            ["git", "-C", self.task["workspace"], "diff", "--quiet", "HEAD"],
            check=True,
            timeout=15,
        )
        status = subprocess.run(
            ["git", "-C", self.task["workspace"], "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        ).stdout
        image = subprocess.run(
            ["docker", "image", "inspect", self.task["container_image"]],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        created = subprocess.run(
            self.command, capture_output=True, text=True, timeout=30
        )
        if created.returncode:
            raise RuntimeError("Docker create failed: " + created.stderr.strip())
        self.container = created.stdout.strip()
        if not re.fullmatch("[0-9a-f]{64}", self.container):
            raise RuntimeError("Docker returned an invalid container ID")
        subprocess.run(
            ["docker", "start", self.container],
            capture_output=True,
            check=True,
            timeout=30,
        )
        return {
            "container_id": self.container,
            "create_command": self.command,
            "image_inspect": json.loads(image.stdout),
            "workspace_snapshot_commit": commit,
            "upstream_base_commit": self.task["base_commit"],
            "initial_git_status": status,
        }

    def invoke(self, name, arguments, folder, timeout_s):
        if not self.container or not self.usable:
            raise RuntimeError("task container is unavailable")
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        python = self.task.get("sandbox_python", "python")
        payload = b""
        if (
            name == "bash"
            and set(arguments) == {"command"}
            and isinstance(arguments["command"], str)
        ):
            command = ["/bin/sh", "-c", arguments["command"]]
        elif (
            name == "read_file"
            and set(arguments) == {"path"}
            and isinstance(arguments["path"], str)
        ):
            command = [python, "-c", FILE_TOOL, "/workspace", arguments["path"], "read"]
        elif (
            name == "write_file"
            and set(arguments) == {"path", "content"}
            and all(isinstance(value, str) for value in arguments.values())
        ):
            payload = arguments["content"].encode()
            if len(payload) > 2 * 1024 * 1024:
                raise ValueError("write_file content exceeds 2 MiB")
            command = [
                python,
                "-c",
                FILE_TOOL,
                "/workspace",
                arguments["path"],
                "write",
            ]
        else:
            raise ValueError("unknown tool or invalid arguments")
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=False)
        started = time.monotonic_ns()
        reason = None
        stdout_path, stderr_path = folder / "stdout.bin", folder / "stderr.bin"
        with (
            stdout_path.open("wb") as stdout,
            stderr_path.open("wb") as stderr,
            tempfile.TemporaryFile() as stdin,
        ):
            stdin.write(payload)
            stdin.seek(0)
            process = subprocess.Popen(
                [
                    "docker",
                    "exec",
                    "-i",
                    "--workdir",
                    "/workspace",
                    self.container,
                    *command,
                ],
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
            )
            while process.poll() is None:
                if time.monotonic_ns() - started > timeout_s * 1e9:
                    reason = "tool_timeout"
                if (
                    stdout_path.stat().st_size + stderr_path.stat().st_size
                    > 10 * 1024 * 1024
                ):
                    reason = "tool_output_safety_limit"
                if reason:
                    # Killing the container also stops descendants of docker exec.
                    subprocess.run(
                        ["docker", "kill", self.container],
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )
                    self.usable = False
                    process.kill()
                    break
                time.sleep(0.02)
            process.wait(timeout=10)
        stdout, stderr = stdout_path.read_bytes(), stderr_path.read_bytes()
        return {
            "exit_code": process.returncode,
            "failure": reason,
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
            "started_monotonic_ns": started,
            "ended_monotonic_ns": time.monotonic_ns(),
            "stdout_bytes": len(stdout),
            "stderr_bytes": len(stderr),
            "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        }

    def close(self):
        if self.container:
            result = subprocess.run(
                ["docker", "rm", "-f", self.container],
                capture_output=True,
                text=True,
                timeout=20,
            )
            return {
                "container_id": self.container,
                "removed": result.returncode == 0,
                "returncode": result.returncode,
                "stderr": result.stderr,
            }
        return {"removed": True, "container_id": None}
