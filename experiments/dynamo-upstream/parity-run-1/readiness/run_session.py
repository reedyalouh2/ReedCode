#!/usr/bin/env python3
"""Run the three saved user turns through a shared capture and request budget."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from capture_proxy import CaptureServer
from check_clients import base_environment, launch_command, sandbox_profile, sha256
from stub_server import StubServer


def captured_requests(output):
    return [json.loads(path.read_text()) for path in sorted(output.glob("*/metadata.json"))]


def all_admitted_finished(records, admitted):
    finished = {row["admitted_inference_count"] for row in records
                if row.get("inference") and row.get("request_sha256") and row.get("ended_monotonic_ns")}
    return admitted > 0 and all(index in finished for index in range(1, admitted + 1))


def session_id(path):
    for line in path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and (event.get("session_id") or event.get("thread_id")):
            return event.get("session_id") or event["thread_id"]
    return None


def signal_client(process, value):
    try:
        os.killpg(process.pid, value)
    except ProcessLookupError:
        pass


def run_turn(command, env, cwd, output, capture, proxy, deadline):
    output.mkdir()
    started = time.monotonic_ns()
    intervention = None
    with (output / "stdout.jsonl").open("wb") as stdout, (output / "stderr.txt").open("wb") as stderr:
        process = subprocess.Popen(command, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                                   stdout=stdout, stderr=stderr, start_new_session=True)
        interrupted = None
        while process.poll() is None:
            now = time.monotonic()
            if now >= deadline:
                intervention = {"reason": "shared_time_limit", "signal": "SIGKILL",
                                "at_monotonic_ns": time.monotonic_ns()}
                signal_client(process, signal.SIGKILL)
                break
            records = captured_requests(capture)
            if any(row.get("unexpected_credentials") for row in records):
                intervention = {"reason": "unexpected_credentials", "signal": "SIGKILL",
                                "at_monotonic_ns": time.monotonic_ns()}
                signal_client(process, signal.SIGKILL)
                break
            with proxy.lock:
                admitted, cap = proxy.calls, proxy.max_calls
            if interrupted is None and admitted >= cap and all_admitted_finished(records, admitted):
                failures = [row["sequence"] for row in records if row.get("request_sha256")
                            and row.get("inference") and not row.get("complete")]
                intervention = {"reason": "phase_call_limit", "signal": "SIGINT",
                                "at_monotonic_ns": time.monotonic_ns(), "admitted_calls": admitted,
                                "failed_http_capture_sequences": failures}
                signal_client(process, signal.SIGINT)
                interrupted = now
            elif interrupted is not None and now - interrupted >= 5:
                intervention["cleanup_signal"] = "SIGKILL"
                signal_client(process, signal.SIGKILL)
                break
            time.sleep(0.025)
        code = process.wait()
    return {"command": command, "exit_code": code, "started_monotonic_ns": started,
            "ended_monotonic_ns": time.monotonic_ns(), "intervention": intervention,
            "session_id": session_id(output / "stdout.jsonl")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", choices=("claude", "codex"), required=True)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--prompt-dir", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--upstream", default="http://127.0.0.1:18002")
    parser.add_argument("--port", type=int, default=18001)
    parser.add_argument("--seconds", type=float, default=1500)
    parser.add_argument("--max-calls", type=int, default=15)
    parser.add_argument("--first-turn-calls", type=int, default=7)
    parser.add_argument("--second-turn-calls", type=int, default=4)
    parser.add_argument("--container-isolated", action="store_true")
    parser.add_argument("--deny-read", type=Path, action="append", default=[])
    parser.add_argument("--trace-id-prefix", help="Observer tracing header; existing client IDs are preserved")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--stub", action="store_true")
    args = parser.parse_args()
    if min(args.seconds, args.max_calls, args.first_turn_calls, args.second_turn_calls) <= 0:
        parser.error("budgets must be positive")
    if any(not path.is_absolute() for path in args.deny_read):
        parser.error("--deny-read paths must be absolute")
    workspace = args.workspace.resolve()
    if not (workspace / "task").is_dir():
        parser.error("workspace/task must be the prepared clean task checkout")
    if args.client == "claude" and sys.platform != "darwin":
        parser.error("this packet pins the macOS Claude executable")
    if args.client == "codex" and (sys.platform == "darwin" or not args.container_isolated
                                   or not Path("/.dockerenv").is_file()):
        parser.error("Codex requires the prepared isolated Linux container")
    output = workspace / "session"
    output.mkdir(exist_ok=False)
    env = base_environment(workspace)
    env["UV_CACHE_DIR"] = str(workspace / "uv-cache")
    profile = sandbox_profile(workspace, args.deny_read) if args.client == "claude" else None
    isolation = ["/usr/bin/sandbox-exec", "-f", str(profile)] if profile else []
    stub = None
    if args.stub:
        if args.client == "codex":
            active = {item.name for item in Path("/sys/class/net").iterdir()
                      if item.is_dir() and int((item / "flags").read_text().strip(), 0) & 1}
            if active != {"lo"}:
                parser.error("Codex stub mode requires --network none")
        fixture = workspace / "task/fixture.txt"
        if fixture.exists():
            parser.error("stub fixture already exists; use a new temporary workspace")
        fixture.write_text("PARITY_STUB_FIXTURE\n")
        stub = StubServer(fixture)
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        args.upstream = f"http://127.0.0.1:{stub.server_port}"
    total_cap = 1 if args.smoke else args.max_calls
    proxy = CaptureServer(("127.0.0.1", args.port), args.upstream, output / "capture", total_cap, args.seconds,
                          args.trace_id_prefix)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    base = launch_command(args.client, args.binary, workspace, proxy.server_port, env)[:-1]
    if args.client == "claude":
        position = base.index("Read")
        base[position:position + 1] = ["Read", "Edit", "Write", "Bash"]
    version = subprocess.check_output(isolation + [args.binary, "--version"], env=env,
                                      cwd=workspace / "task", text=True).strip()
    expected = "2.1.81 (Claude Code)" if args.client == "claude" else "codex-cli 0.155.1"
    if version != expected:
        raise RuntimeError("Client version mismatch: " + version)
    prompts = (["Reply with READY. Do not use tools."] if args.smoke else
               ["Read fixture.txt using a normal file or shell tool, then briefly report its contents.",
                "First follow-up: confirm the fixture marker again.",
                "Second follow-up: give the fixture marker once more."] if args.stub else
               [(args.prompt_dir / f"user-{index}.txt").read_text() for index in (1, 2, 3)])
    result = {"client": args.client, "version": version, "executable_sha256": sha256(args.binary),
              "workspace": str(workspace), "stub_only": args.stub, "smoke": args.smoke,
              "max_calls": total_cap, "seconds": args.seconds, "upstream": args.upstream,
              "trace_id_prefix": args.trace_id_prefix,
              "model_requests_made": 0 if args.stub else "see admitted request ledger",
              "turns": [], "session_id": None, "completed": False}
    packet = json.loads((Path(__file__).resolve().parent / "launch-packet.json").read_text())
    if result["executable_sha256"] != packet[args.client]["executable_sha256"]:
        raise RuntimeError("Client executable hash differs from the pinned launch packet")
    started = time.monotonic()
    deadline = started + args.seconds
    with proxy.lock:
        proxy.first_inference = started
    try:
        for index, prompt in enumerate(prompts):
            allowance = (1 if args.smoke else args.first_turn_calls if index == 0 else
                         args.second_turn_calls if index == 1 else total_cap - proxy.calls)
            if proxy.calls >= total_cap or time.monotonic() >= deadline:
                result["stop_reason"] = "shared_budget_exhausted"
                break
            with proxy.lock:
                proxy.max_calls = min(total_cap, proxy.calls + allowance)
            resume = ([] if index == 0 else ["--resume", result["session_id"]] if args.client == "claude"
                      else ["resume", result["session_id"]])
            invocation = isolation + base + resume + [prompt]
            turn = run_turn(invocation, env, workspace / "task", output / f"turn-{index + 1}",
                            output / "capture", proxy, deadline)
            turn["prompt_sha256"] = hashlib.sha256(prompt.encode()).hexdigest()
            (output / f"turn-{index + 1}/prompt.txt").write_text(prompt)
            turn["followup_delay_seconds"] = (None if index == 0 else
                (turn["started_monotonic_ns"] - result["turns"][-1]["ended_monotonic_ns"]) / 1e9)
            result["turns"].append(turn)
            if index == 0:
                result["session_id"] = turn["session_id"]
            if not result["session_id"]:
                result["stop_reason"] = "missing_session_id"
                break
            if turn["session_id"] and turn["session_id"] != result["session_id"]:
                result["stop_reason"] = "resume_changed_session_id"
                break
            if turn["exit_code"] != 0 and not (turn["intervention"] and
                                               turn["intervention"]["reason"] == "phase_call_limit"):
                result["stop_reason"] = "client_failure"
                break
    finally:
        proxy.shutdown()
        with proxy.lock:
            outstanding = len(proxy.active)
        result["requests_cancelled_at_shutdown"] = outstanding
        if outstanding:
            proxy.cancel_active()
            for _ in range(100):
                with proxy.lock:
                    if not proxy.active:
                        break
                time.sleep(0.01)
        proxy.server_close()
        if stub:
            stub.shutdown()
            stub.server_close()
        result["admitted_calls"] = proxy.calls
        result["elapsed_seconds"] = time.monotonic() - started
        result["completed"] = len(result["turns"]) == len(prompts) and not result.get("stop_reason")
        result["capture_ledger"] = captured_requests(output / "capture")
        result["all_admitted_requests_recorded"] = all_admitted_finished(result["capture_ledger"], proxy.calls)
        if not result["all_admitted_requests_recorded"]:
            result["completed"] = False
            result["stop_reason"] = "unfinished_capture"
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in ("client", "session_id", "admitted_calls", "completed")}, indent=2))
    raise SystemExit(0 if result["completed"] else 1)


if __name__ == "__main__":
    main()
