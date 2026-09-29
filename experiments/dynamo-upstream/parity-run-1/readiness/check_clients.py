#!/usr/bin/env python3
"""Exercise the installed CLIs against local scripted SSE responses."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from capture_proxy import CaptureServer
from stub_server import MODEL, REASONING, StubServer


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sandbox_profile(root, deny_extra=()):
    personal = Path(os.environ["HOME"])
    denied = [personal / name for name in (".codex", ".claude", ".agents", ".config", ".ssh", "Library/Keychains",
                                          ".runpod", ".aws", ".azure", ".kube", ".docker", ".huggingface")]
    denied += [Path(name) for name in ("/tmp/reedcode-gpu-credentials", "/tmp/reedcode-runpod-auth",
                                      "/tmp/reedcode-prefix-auth")]
    denied += list(map(Path, deny_extra))
    lines = ['(version 1)', '(allow default)', '(deny network*)',
             '(allow network-outbound (remote ip "localhost:*"))',
             '(deny file-write*)', f'(allow file-write* (subpath {json.dumps(str(root))}) (literal "/dev/null") (literal "/dev/tty"))',
             '(deny mach-lookup (global-name "com.apple.securityd"))']
    denied = sorted({str(path.resolve()) for path in denied} | {str(path) for path in denied})
    lines.extend(f'(deny file-read-data (subpath {json.dumps(path)}))' for path in denied)
    for name in (".claude.json", ".netrc", ".git-credentials", ".npmrc", ".pypirc", ".cache/huggingface/token"):
        lines.append(f'(deny file-read-data (literal {json.dumps(str(personal / name))}))')
    profile = root / "client.sb"
    profile.write_text("\n".join(lines) + "\n")
    return profile


def base_environment(root):
    result = {key: os.environ[key] for key in ("HOME", "USER", "LOGNAME", "PATH", "SHELL", "LANG")
              if key in os.environ}
    result.update(TMPDIR=str(root / "tmp"), TERM="dumb", NO_COLOR="1")
    (root / "tmp").mkdir()
    return result


def launch_command(client, binary, root, port, env):
    origin = f"http://127.0.0.1:{port}"
    prompt = "Read fixture.txt using a normal file or shell tool, then briefly report its contents."
    if client == "claude":
        env.update(CLAUDE_CONFIG_DIR=str(root / "claude-state"), ANTHROPIC_BASE_URL=origin,
                   ANTHROPIC_AUTH_TOKEN="local-dynamo", ANTHROPIC_MODEL=MODEL,
                   ANTHROPIC_DEFAULT_SONNET_MODEL=MODEL, ANTHROPIC_DEFAULT_OPUS_MODEL=MODEL,
                   ANTHROPIC_DEFAULT_HAIKU_MODEL=MODEL, MAX_THINKING_TOKENS="4096",
                   CLAUDE_CODE_MAX_OUTPUT_TOKENS="8192", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
        return [binary, "--print", "--verbose", "--output-format", "stream-json",
                "--include-partial-messages", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--setting-sources", "", "--settings", '{"alwaysThinkingEnabled":true}',
                "--allowedTools", "Read", "--no-chrome", prompt]
    values = {
        "model": json.dumps(MODEL), "model_provider": '"dynamo_parity"',
        "model_context_window": "32768",
        "model_reasoning_effort": '"medium"', "web_search": '"disabled"',
        "model_providers.dynamo_parity.name": '"Dynamo parity"',
        "model_providers.dynamo_parity.base_url": json.dumps(origin + "/v1"),
        "model_providers.dynamo_parity.wire_api": '"responses"',
        "model_providers.dynamo_parity.requires_openai_auth": "false",
        "model_providers.dynamo_parity.supports_websockets": "false",
        "model_providers.dynamo_parity.request_max_retries": "0",
        "model_providers.dynamo_parity.stream_max_retries": "0",
        "sqlite_home": json.dumps(str(root / "codex-state")),
        "log_dir": json.dumps(str(root / "codex-logs")),
        "analytics.enabled": "false", "feedback.enabled": "false",
        "allow_login_shell": "false",
    }
    command = [binary, "exec", "--ignore-user-config", "--ignore-rules", "--strict-config",
               "--json", "--sandbox", "workspace-write", "--skip-git-repo-check", "--cd", str(root / "task")]
    if sys.platform == "darwin":
        command.append("--ephemeral")
    for key, value in values.items():
        command += ["-c", key + "=" + value]
    return command + [prompt]


def summarize(client, requests, records, exit_code, timed_out, stdout):
    bodies = [request["body"] for request in requests
              if request["path"].split("?", 1)[0] in ("/v1/messages", "/v1/responses")]
    tool_results = []
    for body in bodies:
        if client == "claude":
            tool_results += [block for msg in body.get("messages", []) if isinstance(msg.get("content"), list)
                             for block in msg["content"] if block.get("type") == "tool_result"]
        else:
            tool_results += [item for item in body.get("input", []) if isinstance(item, dict)
                             and item.get("type") == "function_call_output"]
    return {
        "client": client, "exit_code": exit_code, "timed_out": timed_out,
        "inference_requests": len(bodies), "paths": sorted({r["path"] for r in requests}),
        "models": sorted({body.get("model", "") for body in bodies}),
        "first_request_stream": bodies[0].get("stream") if bodies else None,
        "first_request_tool_count": len(bodies[0].get("tools", [])) if bodies else None,
        "first_request_thinking": bodies[0].get("thinking") if bodies else None,
        "first_request_reasoning": bodies[0].get("reasoning") if bodies else None,
        "first_request_max_tokens": bodies[0].get("max_tokens") if bodies else None,
        "thinking_echoed_on_tool_followup": any(REASONING in json.dumps(body) for body in bodies[1:]),
        "tool_result_history_occurrences": len(tool_results),
        "unique_tool_result_ids": len({item.get("tool_use_id") or item.get("call_id") for item in tool_results}),
        "tool_result_contains_fixture": any("PARITY_STUB_FIXTURE" in json.dumps(item) for item in tool_results),
        "unexpected_credentials": any(record.get("unexpected_credentials") for record in records),
        "request_bytes_match_upstream": all(any(record.get("request_sha256") == hashlib.sha256(r["raw"]).hexdigest()
                                                   for record in records) for r in requests),
        "received_final_answer": "The local protocol check is complete." in stdout,
        "complete_captures": all(record["complete"] for record in records),
    }


def check(client, binary, output, timeout, container_isolated=False):
    root = Path(tempfile.mkdtemp(prefix=f"reedcode-parity-stub-{client}-", dir="/tmp")).resolve()
    (root / "task").mkdir()
    fixture = root / "task" / "fixture.txt"
    fixture.write_text("PARITY_STUB_FIXTURE\n")
    if sys.platform != "darwin" and not container_isolated:
        raise RuntimeError("Linux checks require an explicitly isolated --network none container")
    if sys.platform != "darwin":
        active_interfaces = {p.name for p in Path("/sys/class/net").iterdir()
                             if p.is_dir() and int((p / "flags").read_text().strip(), 0) & 1}
        if not Path("/.dockerenv").is_file() or active_interfaces != {"lo"}:
            raise RuntimeError("Expected a Docker container with only loopback active")
    profile = sandbox_profile(root) if sys.platform == "darwin" else None
    env = base_environment(root)
    stub = StubServer(fixture)
    proxy = CaptureServer(("127.0.0.1", 0), f"http://127.0.0.1:{stub.server_port}", root / "capture", 15, timeout)
    for server in (stub, proxy):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    command = launch_command(client, binary, root, proxy.server_port, env)
    executable = Path(binary).resolve()
    version_prefix = ["/usr/bin/sandbox-exec", "-f", str(profile)] if profile else []
    version = subprocess.check_output(version_prefix + [binary, "--version"], env=env,
                                      cwd=root / "task", text=True).strip()
    expected_version = "2.1.81 (Claude Code)" if client == "claude" else "codex-cli 0.155.1"
    if version != expected_version:
        raise RuntimeError(f"Expected {expected_version}, got {version}")
    deadline = time.monotonic() + timeout

    def run_turn(turn_command):
        invocation = (["/usr/bin/sandbox-exec", "-f", str(profile)] if profile else []) + turn_command
        process = subprocess.Popen(invocation, cwd=root / "task", env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
        return process.returncode, timed_out, stdout, stderr

    exit_code, timed_out, stdout, stderr = run_turn(command)
    turns = [{"command": command, "exit_code": exit_code, "timed_out": timed_out,
              "stdout": stdout.decode(errors="replace"), "stderr": stderr.decode(errors="replace")}]
    session_id = None
    for line in stdout.decode(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        session_id = session_id or event.get("session_id") or event.get("thread_id")
    if exit_code == 0 and session_id and "--ephemeral" not in command:
        for followup in ("First follow-up: confirm the fixture marker again.",
                         "Second follow-up: give the fixture marker once more."):
            resume = (["--resume", session_id] if client == "claude" else ["resume", session_id])
            resume_command = command[:-1] + resume + [followup]
            turn_code, turn_timeout, turn_out, turn_err = run_turn(resume_command)
            turns.append({"command": resume_command, "exit_code": turn_code, "timed_out": turn_timeout,
                          "stdout": turn_out.decode(errors="replace"), "stderr": turn_err.decode(errors="replace")})
            stdout += turn_out
            stderr += turn_err
            timed_out = timed_out or turn_timeout
            exit_code = turn_code
            if turn_code != 0:
                break
    for server in (proxy, stub):
        server.shutdown()
        server.server_close()
    records = [json.loads(path.read_text()) for path in sorted((root / "capture").glob("*/metadata.json"))]
    summary = summarize(client, stub.requests, records, exit_code, timed_out, stdout.decode(errors="replace"))
    summary.update(version=version, executable=str(executable), executable_sha256=sha256(executable),
                   command=command, temporary_state=str(root), external_network_denied=True,
                   personal_state_reads_denied=True, parent_home_variables_unchanged=True,
                   stub_only=True, model_requests_made=0, session_id=session_id,
                   followup_turns_completed=sum(turn["exit_code"] == 0 for turn in turns[1:]),
                   isolation="macOS sandbox" if profile else "fresh Linux container; network none")
    summary["readiness_passed"] = all((exit_code == 0, not timed_out, summary["models"] == [MODEL],
                                      summary["first_request_stream"], summary["first_request_tool_count"],
                                      summary["thinking_echoed_on_tool_followup"],
                                      summary["tool_result_contains_fixture"],
                                      not summary["unexpected_credentials"],
                                      summary["request_bytes_match_upstream"], summary["complete_captures"],
                                      summary["received_final_answer"], summary["followup_turns_completed"] == 2))
    dest = output / client
    dest.mkdir(parents=True, exist_ok=False)
    shutil.copytree(root / "capture", dest / "capture")
    for name, data in (("stdout.jsonl", stdout), ("stderr.txt", stderr)):
        (dest / name).write_bytes(data)
    if profile:
        shutil.copyfile(profile, dest / "client.sb")
    (dest / "turns.json").write_text(json.dumps(turns, indent=2) + "\n")
    (dest / "result.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", choices=("claude", "codex", "both"), default="both")
    parser.add_argument("--claude", default=str(Path.home() / ".local/bin/claude"))
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--container-isolated", action="store_true")
    args = parser.parse_args()
    summaries = [check(client, getattr(args, client), args.output, args.timeout, args.container_isolated)
                 for client in (("claude", "codex") if args.client == "both" else (args.client,))]
    raise SystemExit(0 if all(result["readiness_passed"] for result in summaries) else 1)


if __name__ == "__main__":
    main()
