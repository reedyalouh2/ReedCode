"""Reset one controlled trial, require cache/index evidence, then replay it."""

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import time
from types import SimpleNamespace

from kv_report import collector, events
from prepare import ROOT, json_bytes, sha
import replay
from verify import verify


def run_hook(argv, condition, output, name):
    if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) or not arg for arg in argv):
        raise ValueError("Hooks must be nonempty argv arrays")
    command = [arg.replace("{condition}", condition) for arg in argv]
    environment = os.environ.copy()
    if name.startswith("worker-reset"):
        environment.pop("DYN_TCP_RPC_PORT", None)
        environment.pop("DYN_TCP_RESPONSE_STREAM_PORT", None)
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, env=environment)
    (output / (name + ".stdout")).write_bytes(result.stdout)
    (output / (name + ".stderr")).write_bytes(result.stderr)
    if result.returncode:
        raise ValueError(name + " failed; see its saved output")
    return json.loads(result.stdout)


def latest_clear(path, after_unix, expected_topic):
    ledger = collector.PublishedBlocks()
    found = None
    for row in events(path, allow_partial=True):
        if row["topic"] != expected_topic:
            raise ValueError("KV topic differs from this trial's collector")
        state = ledger.accept(row["sequence"], row["payload_sha256"], row["batch"])
        if state.get("clear_events") and row["batch"][0] >= after_unix:
            found = {"sequence": row["sequence"], "payload_sha256": row["payload_sha256"],
                     "event_timestamp_unix": row["batch"][0]}
    if found and ledger.complete_since_clear and not ledger.blocks:
        return found
    return None


def wait_clear(path, after_unix, topic, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            found = latest_clear(path, after_unix, topic)
            if found:
                return found
        time.sleep(.1)
    raise TimeoutError("No new clear event with an empty published block set")


def check_frontend_proof(proof):
    if (proof.get("router_index_empty") is not True or type(proof.get("frontend_pid")) is not int
            or proof["frontend_pid"] <= 0 or not proof.get("frontend_epoch")
            or type(proof.get("started_unix")) not in (int, float)):
        raise ValueError("Fresh frontend/index proof is incomplete")
    if proof["frontend_epoch"] == proof.get("previous_frontend_epoch"):
        raise ValueError("Frontend epoch did not change")
    path = Path(proof["proof_file"])
    if sha(path.read_bytes()) != proof.get("proof_sha256"):
        raise ValueError("Frontend empty-index proof file changed")
    return path


def run(args):
    verification = verify(args.directory)
    if not args.execute:
        print(json.dumps({"execute": False, "verification": verification,
            "steps": ["idle worker reset", "one-token canary", "new clear event and empty published set",
                      "fresh frontend and actual empty-index proof", "new-frontend clear confirmation",
                      "frozen replay with metrics and identity snapshots", "closing reset/canary event barrier",
                      "save the complete event slice"]}, indent=2))
        return
    hooks = json.loads(args.hooks.read_text())
    capture = json.loads(args.capture_proof.read_text())
    config = json.loads((args.kv_directory / "config.json").read_text())
    if config.get("hostname") != socket.gethostname():
        raise ValueError("Run replay and KV collector on the same pod")
    if (capture.get("token_capture_ready") is not True or capture.get("kv_event_capture_ready") is not True
            or capture.get("worker_epoch") != config.get("operator_supplied_worker_epoch")):
        raise ValueError("Capture readiness and worker epoch must match the running collector")
    args.output.mkdir(parents=True, exist_ok=False)
    evidence = {"status": "reset_started", "session": args.session, "condition": args.condition,
                "repeat": args.repeat, "started_unix": time.time(), "hostname": socket.gethostname(),
                "hooks_sha256": sha(args.hooks.read_bytes()), "capture_proof_sha256": sha(args.capture_proof.read_bytes()),
                "capture_proof": capture, "steps": []}
    state_path = args.output / "trial.json"
    try:
        trial_id = f"prefill-{args.session}-{args.condition}-r{args.repeat}"
        before_reset = time.time()
        reset = run_hook(hooks["worker_reset"], args.condition, args.output, "worker-reset")
        if reset.get("backend_reset_reported") is not True:
            raise ValueError("Worker did not confirm its reset")
        canary = {"payload": {"model": "Qwen/Qwen3-8B", "prompt": [42], "max_tokens": 1,
                               "temperature": 0, "stream": True, "stream_options": {"include_usage": True}}}
        result, _ = replay.send(args.base_url.rstrip("/") + "/completions", canary,
                                trial_id + "-reset-canary", args.output, 15)
        if result["cached_tokens"] != 0:
            raise ValueError("Reset canary unexpectedly reused tokens")
        first_clear = wait_clear(args.kv_directory / "frames.jsonl", before_reset, config["topic"])
        evidence["steps"].append({"backend_reset": reset, "canary": result, "first_clear": first_clear})
        frontend = run_hook(hooks["fresh_frontend"], args.condition, args.output, "fresh-frontend")
        proof_path = check_frontend_proof(frontend)
        (args.output / "frontend-empty-index.proof").write_bytes(proof_path.read_bytes())
        # The launcher emits another reset/canary after the new frontend is listening.
        clear = wait_clear(args.kv_directory / "frames.jsonl", frontend["started_unix"], config["topic"])
        evidence.update(frontend=frontend, clear_event=clear)
        reset_record = {"backend_cache_empty": True, "router_index_empty": True,
                        "token_capture_ready": True, "kv_event_capture_ready": True,
                        "process_epoch": {"worker": capture["worker_epoch"], "frontend": frontend["frontend_epoch"]},
                        "clear_event": clear, "frontend_proof_sha256": frontend["proof_sha256"]}
        reset_path = args.output / "reset-verified.json"
        reset_path.write_bytes(json_bytes(reset_record))
        evidence["status"] = "reset_verified"
        state_path.write_bytes(json_bytes(evidence))
        replay.run(SimpleNamespace(directory=args.directory, session=args.session, condition=args.condition,
            repeat=args.repeat, execute=True, base_url=args.base_url, reset_record=reset_path,
            output=args.output / "replay", max_seconds=args.max_seconds,
            metrics_url=args.metrics_url, identity_url=args.identity_url))
        before_close = time.time()
        closing_reset = run_hook(hooks["worker_reset"], args.condition, args.output, "worker-reset-closing")
        if closing_reset.get("backend_reset_reported") is not True:
            raise ValueError("Worker did not confirm the closing reset")
        closing_canary, _ = replay.send(args.base_url.rstrip("/") + "/completions", canary,
                                        trial_id + "-closing-canary", args.output, 15)
        if closing_canary["cached_tokens"] != 0:
            raise ValueError("Closing canary unexpectedly reused tokens")
        closing_clear = wait_clear(args.kv_directory / "frames.jsonl", before_close, config["topic"])
        rows = events(args.kv_directory / "frames.jsonl", allow_partial=True)
        index = next(i for i, row in enumerate(rows) if row["sequence"] == clear["sequence"] and row["payload_sha256"] == clear["payload_sha256"])
        last = next(i for i, row in enumerate(rows) if row["sequence"] == closing_clear["sequence"] and row["payload_sha256"] == closing_clear["payload_sha256"])
        chosen = rows[index:last + 1]
        (args.output / "kv-frames.jsonl").write_text("".join(json.dumps({k: v for k, v in row.items() if k not in ("batch", "topic")}) + "\n" for row in chosen))
        (args.output / "kv-config.json").write_bytes((args.kv_directory / "config.json").read_bytes())
        evidence.update(status="replay_completed", last_captured_sequence=chosen[-1]["sequence"],
                        closing_clear=closing_clear, closing_reset=closing_reset, closing_canary=closing_canary)
    except Exception as error:
        evidence.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        evidence["ended_unix"] = time.time()
        state_path.write_bytes(json_bytes(evidence))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / "fixtures")
    parser.add_argument("--session", choices=("short", "long"), required=True)
    parser.add_argument("--condition", choices=("off", "stock", "fixed"), required=True)
    parser.add_argument("--repeat", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--metrics-url", default="http://127.0.0.1:8081/metrics")
    parser.add_argument("--identity-url", default="http://127.0.0.1:9099/identity")
    parser.add_argument("--max-seconds", type=float, default=120)
    parser.add_argument("--hooks", type=Path)
    parser.add_argument("--capture-proof", type=Path)
    parser.add_argument("--kv-directory", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.execute and not all((args.hooks, args.capture_proof, args.kv_directory, args.output)):
        parser.error("Execution requires hooks, capture proof, a KV capture directory and a fresh output directory")
    if not 0 < args.max_seconds <= 600:
        parser.error("max-seconds must be in (0, 600]")
    run(args)
