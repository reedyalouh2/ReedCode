"""Run the stock Dynamo speculative-prefill off/on feasibility pilot."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
from urllib.parse import urlsplit

from run_experiments import ROOT, REAL_TASKS, export_run, prepare_tasks, save_manifest, tree_hash


def build_plan(repeats=5, seed=20260927, tasks=("noisy-bugfix",)):
    if repeats < 1 or not tasks or len(set(tasks)) != len(tasks):
        raise ValueError("Use distinct tasks and positive repeats")
    rng = random.Random(seed)
    blocks = []
    for task in tasks:
        orders = [("off", "on"), ("on", "off")] * ((repeats + 1) // 2)
        rng.shuffle(orders)
        blocks.extend((task, repeat, order) for repeat, order in enumerate(orders[:repeats], 1))
    if len(tasks) > 1:
        rng.shuffle(blocks)
    return [{"run_id": f"{task}_r{repeat:02d}_{condition}", "repeat": repeat,
             "position": position, "condition": condition, "task": task,
             "cap_chars": 20000, "output_policy": "head"}
            for task, repeat, order in blocks
            for position, condition in enumerate(order, 1)]


def trial_env(base_url, budget, condition, capture=False):
    env = dict(os.environ)
    # Speculative requests invalidate the old one-call counter attribution.
    env.pop("VLLM_METRICS_URL", None)
    env.update(OPENAI_BASE_URL=base_url, MODEL_API="chat",
               DYNAMO_SPECULATIVE_PREFILL=condition, MAX_OUTPUT_TOKENS=str(budget),
               MAX_TURNS="30", TOOL_TIMEOUT="120", MAX_TOOL_OUTPUT="20000", OUTPUT_POLICY="head",
               DYNAMO_CAPTURE_REQUESTS="1" if capture else "0")
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def validate_url(value):
    url = urlsplit(value)
    if (url.scheme not in ("http", "https") or not url.hostname or url.username
            or url.password or url.query or url.fragment):
        raise ValueError("Use an HTTP(S) URL without credentials, query, or fragment")


def read_deployment(path):
    deployment = json.loads(path.read_text())
    required = ("dynamo_revision", "backend_version", "model_revision", "gpu",
                "dtype", "context_limit", "cache_policy", "launch_command")
    if not isinstance(deployment, dict) or any(not deployment.get(k) for k in required):
        raise ValueError("Deployment JSON requires: " + ", ".join(required))
    return deployment


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="Exact model name served by Dynamo")
    parser.add_argument("--base-url", help="Dynamo frontend URL, e.g. http://127.0.0.1:8000/v1")
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20260927)
    parser.add_argument("--suite", choices=("synthetic", "real"), default="synthetic")
    parser.add_argument("--tasks", nargs="+", choices=REAL_TASKS)
    parser.add_argument("--capture-requests", action="store_true", help="Save prompts and tool output for replay")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--deployment", type=Path, help="JSON record of the actual GPU deployment")
    parser.add_argument("--execute", action="store_true", help="Run Harbor and make model requests")
    args = parser.parse_args(argv)
    if args.max_output_tokens < 1:
        parser.error("--max-output-tokens must be positive")
    try:
        if args.tasks and args.suite != "real":
            parser.error("--tasks requires --suite real")
        tasks = (args.tasks or REAL_TASKS) if args.suite == "real" else ("noisy-bugfix",)
        plan = build_plan(args.repeats, args.seed, tasks)
    except ValueError as exc:
        parser.error(str(exc))
    deployment = None
    if args.execute:
        if not args.base_url or not args.deployment:
            parser.error("--execute requires --base-url and --deployment")
        if not os.getenv("OPENAI_API_KEY"):
            parser.error("Set OPENAI_API_KEY for this deployment; use unused only if authentication is disabled")
        if not shutil.which("harbor"):
            parser.error("Run with uv run python run_dynamo.py ...")
        try:
            validate_url(args.base_url)
            deployment = read_deployment(args.deployment)
        except (OSError, ValueError) as exc:
            parser.error(f"Cannot read deployment JSON: {type(exc).__name__}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = (args.output or ROOT / "runs" / f"dynamo-{stamp}").resolve()
    output.mkdir(parents=True, exist_ok=False)
    sources = ("run_dynamo.py", "run_experiments.py", "dynamo_support.py", "model_backend.py",
               "output_policy.py", "reedcode_harbor_agent.py", "server_metrics.py", "chat_stream.py",
               "dynamo_report.py", "reporting.py", "uv.lock")
    manifest = {
        "study_kind": "dynamo_feasibility", "schema_version": 1,
        "state": "planned", "model": args.model, "model_api": "chat",
        "max_output_tokens": args.max_output_tokens, "schedule_seed": args.seed,
        "deployment": deployment, "plan": plan, "runs": [],
        "capture_requests": args.capture_requests,
        "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                          for name in sources},
        "limitations": ["Hint application requires server-side verification.",
                        "Client streaming timing includes network and parsing.",
                        "Shared server cache carries between trials; record resets separately.",
                        "Per-call server counter attribution is disabled."],
    }
    save_manifest(output, manifest)
    print(f"{len(plan)} planned trials: head 20K throughout; speculative prefill off/on.")
    print(f"Study directory: {output}")
    if not args.execute:
        print("Plan only. No server contact, Docker, or model requests.")
        return 0

    manifest["tasks"] = prepare_tasks(args.suite, tasks, output)
    manifest["state"] = "preflight"
    save_manifest(output, manifest)

    def command(job_name, agent, task_path):
        cmd = ["harbor", "run", "-p", str(task_path), "--agent", agent,
               "--job-name", job_name, "--jobs-dir", str(ROOT / "jobs"),
               "--n-concurrent", "1", "--n-attempts", "1", "--max-retries", "0"]
        if agent != "oracle":
            cmd.extend(["--model", args.model])
        return cmd

    try:
        manifest["preflight"] = []
        for task in tasks:
            task_path = output / manifest["tasks"][task]["path"]
            oracle_job = f"reedcode-dynamo-{stamp}-oracle-{task}"
            check = subprocess.run(command(oracle_job, "oracle", task_path), cwd=ROOT)
            oracle = export_run(ROOT / "jobs" / oracle_job, output, f"oracle-{task}", task, None,
                                check.returncode)
            manifest["preflight"].append(oracle)
            save_manifest(output, manifest)
            if oracle["reward"] != 1 or oracle["exception_type"]:
                manifest["state"] = "preflight_failed"
                return 1
        manifest["state"] = "running"
        for planned in plan:
            snapshot = manifest["tasks"][planned["task"]]
            task_path = output / snapshot["path"]
            if tree_hash(task_path) != snapshot["sha256"]:
                raise RuntimeError("Task snapshot changed during the study")
            job = f"reedcode-dynamo-{stamp}-{planned['run_id']}"
            row = {**planned, "status": "running", "reward": None,
                   "exception_type": "UnfinishedAttempt"}
            manifest["runs"].append(row)
            save_manifest(output, manifest)
            result = subprocess.run(command(job, "reedcode_harbor_agent:ReedCodeAgent", task_path),
                                    cwd=ROOT, env=trial_env(args.base_url, args.max_output_tokens,
                                                          planned["condition"], args.capture_requests))
            row.update(export_run(ROOT / "jobs" / job, output, planned["run_id"], planned["task"],
                                  20000, result.returncode), status="finished")
            save_manifest(output, manifest)
        manifest["state"] = "finished"
    except (Exception, KeyboardInterrupt):
        manifest["state"] = "interrupted"
        raise
    finally:
        save_manifest(output, manifest)
        from dynamo_report import report_pilot
        report_pilot(output)
    print("Attempts and verifier outcomes saved. Inspect server traces before interpreting hint effects.")
    return int(any(r["exception_type"] or r["reward"] is None or not r["trace"] for r in manifest["runs"]))


if __name__ == "__main__":
    raise SystemExit(main())
