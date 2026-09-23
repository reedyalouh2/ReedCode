"""Run repeated, interleaved output-retention comparisons."""

import argparse
from datetime import datetime, timezone
import hashlib
import itertools
import json
import os
import random
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
REAL_TASKS = (
    "regex-log", "extract-elf", "sanitize-git-repo", "build-cython-ext",
    "build-pmars", "cancel-async-tasks", "custom-memory-heap-crash",
    "fix-git", "log-summary-date-ranges", "large-scale-text-editing",
)
CONDITIONS = {
    "head_20k": (20000, "head"),
    "head_2k": (2000, "head"),
    "head_tail_2k": (2000, "head_tail"),
}


def build_plan(tasks, repeats=5, seed=20260922):
    if repeats < 1 or not tasks or len(set(tasks)) != len(tasks):
        raise ValueError("Use distinct tasks and a positive repetition count")
    rng = random.Random(seed)
    blocks = []
    for task in tasks:
        orders = []
        for repeat in range(1, repeats + 1):
            if not orders:
                orders = list(itertools.permutations(CONDITIONS))
                rng.shuffle(orders)
            blocks.append((task, repeat, orders.pop()))
    rng.shuffle(blocks)
    plan = []
    for block, (task, repeat, conditions) in enumerate(blocks, 1):
        for position, condition in enumerate(conditions, 1):
            cap, policy = CONDITIONS[condition]
            plan.append({
                "run_id": f"b{block:03d}_r{repeat}_{condition}",
                "task": task, "repeat": repeat, "block": block,
                "position": position, "condition": condition,
                "cap_chars": cap, "output_policy": policy,
            })
    return plan


def tree_hash(path):
    digest = hashlib.sha256()
    for file in sorted(path.rglob("*")):
        if file.is_file():
            digest.update(file.relative_to(path).as_posix().encode() + b"\0")
            digest.update(hashlib.sha256(file.read_bytes()).digest())
    return digest.hexdigest()


def save_manifest(output, manifest):
    pending = output / "manifest.tmp"
    pending.write_text(json.dumps(manifest, indent=2) + "\n")
    pending.replace(output / "manifest.json")


def prepare_tasks(suite, tasks, output):
    snapshots = {}
    for task in tasks:
        parent = output / "tasks" / task
        parent.mkdir(parents=True)
        if suite == "synthetic":
            path = parent / task
            shutil.copytree(ROOT / "evals" / task, path,
                            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        else:
            subprocess.run(["harbor", "tasks", "download", f"terminal-bench/{task}",
                            "--output-dir", str(parent)], check=True, cwd=ROOT)
            paths = list(parent.glob("*/task.toml"))
            if len(paths) != 1:
                raise RuntimeError(f"Expected one downloaded task: {task}")
            path = paths[0].parent
        snapshots[task] = {"path": str(path.relative_to(output)),
                           "sha256": tree_hash(path)}
    return snapshots


def export_run(job, output, run_id, task, cap, returncode):
    row = {
        "run_id": run_id, "task": task, "cap_chars": cap,
        "trace": None, "trace_sha256": None, "job_name": job.name,
        "reward": None, "exception_type": None, "job_runtime_s": None,
        "harbor_returncode": returncode,
    }
    trials = list(job.glob("*/result.json"))
    if len(trials) == 1:
        trial = json.loads(trials[0].read_text())
        row.update({
            "trial_name": trial.get("trial_name"),
            "task_checksum": trial.get("task_checksum"),
            "agent_info": trial.get("agent_info"),
            "reward": (trial.get("verifier_result") or {}).get("rewards", {}).get("reward"),
            "exception_type": (trial.get("exception_info") or {}).get("exception_type"),
        })
        traces = list(trials[0].parent.rglob("reedcode_trace.jsonl"))
        if len(traces) == 1:
            target = output / f"{run_id}.jsonl"
            shutil.copyfile(traces[0], target)
            row.update(trace=target.name, trace_sha256=hashlib.sha256(target.read_bytes()).hexdigest())
    else:
        row["exception_type"] = "MissingOrAmbiguousTrialResult"
    if returncode and not row["exception_type"]:
        row["exception_type"] = "HarborProcessError"
    if (job / "result.json").exists():
        data = json.loads((job / "result.json").read_text())
        row.update(job_started_at=data.get("started_at"), job_finished_at=data.get("finished_at"))
        if row["job_started_at"] and row["job_finished_at"]:
            row["job_runtime_s"] = (
                datetime.fromisoformat(row["job_finished_at"])
                - datetime.fromisoformat(row["job_started_at"])
            ).total_seconds()
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=("synthetic", "real"))
    parser.add_argument("--model", default=os.getenv("MODEL", "gpt-5.6-terra"))
    parser.add_argument("--output", type=Path, help="New output directory (must not exist)")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20260922, help="Schedule seed, not a model seed")
    parser.add_argument("--tasks", nargs="+", choices=REAL_TASKS, help="Real-suite subset")
    parser.add_argument("--dry-run", action="store_true", help="Save the schedule without downloads or model calls")
    parser.add_argument("--check-only", action="store_true", help="Prepare task snapshots and run their oracles, without model calls")
    args = parser.parse_args()
    if args.tasks and args.suite != "real":
        parser.error("--tasks is only available for the real suite")
    if args.dry_run and args.check_only:
        parser.error("Choose either --dry-run or --check-only")
    tasks = (args.tasks or REAL_TASKS) if args.suite == "real" else ("noisy-bugfix",)
    try:
        plan = build_plan(tasks, args.repeats, args.seed)
    except ValueError as exc:
        parser.error(str(exc))
    if not args.dry_run:
        if not args.check_only and not os.getenv("OPENAI_API_KEY"):
            parser.error("Set OPENAI_API_KEY before running model evaluations; --dry-run needs no key")
        if not shutil.which("harbor"):
            parser.error("harbor is not on PATH; run with uv run python run_experiments.py ...")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = (args.output or ROOT / "runs" / f"{args.suite}-{stamp}").resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": 2, "model": args.model, "schedule_seed": args.seed,
        "repeats": args.repeats, "state": "planned",
        "protocol": "randomized task/repetition blocks; three adjacent conditions per block",
        "max_turns": 30, "tool_timeout_sec": 120,
        "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                          for name in ("reedcode_harbor_agent.py", "output_policy.py",
                                       "run_experiments.py", "reporting.py", "uv.lock")},
        "plan": plan, "runs": [],
    }
    save_manifest(output, manifest)
    print(f"{len(tasks)} tasks x {args.repeats} repeats x 3 conditions = {len(plan)} trials", flush=True)
    print(f"Schedule and results: {output}", flush=True)
    if args.dry_run:
        return 0
    import importlib.metadata
    manifest["harbor_version"] = importlib.metadata.version("harbor")
    manifest["openai_version"] = importlib.metadata.version("openai")
    manifest["state"] = "preparing"
    save_manifest(output, manifest)
    manifest["tasks"] = prepare_tasks(args.suite, tasks, output)
    manifest["preflight"] = []
    save_manifest(output, manifest)
    for task in tasks:
        path = output / manifest["tasks"][task]["path"]
        job_name = f"reedcode-{stamp}-oracle-{task}"
        result = subprocess.run(["harbor", "run", "-p", str(path), "--agent", "oracle",
            "--job-name", job_name, "--jobs-dir", str(ROOT / "jobs"),
            "--n-concurrent", "1", "--n-attempts", "1", "--max-retries", "0"], cwd=ROOT)
        row = export_run(ROOT / "jobs" / job_name, output, f"oracle-{task}", task,
                         None, result.returncode)
        manifest["preflight"].append(row)
        save_manifest(output, manifest)
    if any(r["reward"] != 1 or r["exception_type"] for r in manifest["preflight"]):
        manifest["state"] = "preflight_failed"
        save_manifest(output, manifest)
        print("Oracle preflight failed; no model calls were made.", flush=True)
        return 1
    if args.check_only:
        manifest["state"] = "preflight_passed"
        save_manifest(output, manifest)
        print("Oracle preflight passed; no model calls were made.", flush=True)
        return 0
    manifest["state"] = "running"
    save_manifest(output, manifest)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.update(MAX_TURNS="30", TOOL_TIMEOUT="120")
    for planned in plan:
        run_id, task = planned["run_id"], planned["task"]
        snapshot = manifest["tasks"][task]
        task_path = output / snapshot["path"]
        if tree_hash(task_path) != snapshot["sha256"]:
            raise RuntimeError(f"Task snapshot changed during the experiment: {task}")
        job_name = f"reedcode-{args.suite}-{stamp}-{run_id}"
        command = ["harbor", "run", "-p", str(task_path),
                   "--agent", "reedcode_harbor_agent:ReedCodeAgent", "--model", args.model,
                   "--job-name", job_name, "--jobs-dir", str(ROOT / "jobs"),
                   "--n-concurrent", "1", "--n-attempts", "1", "--max-retries", "0"]
        manifest["runs"].append({**planned, "status": "running", "reward": None,
                                 "exception_type": "UnfinishedAttempt"})
        save_manifest(output, manifest)
        print(f"Running {run_id}: {task}", flush=True)
        try:
            result = subprocess.run(command, cwd=ROOT, env={**env,
                "MAX_TOOL_OUTPUT": str(planned["cap_chars"]),
                "OUTPUT_POLICY": planned["output_policy"]})
            row = export_run(ROOT / "jobs" / job_name, output, run_id, task,
                             planned["cap_chars"], result.returncode)
        except (OSError, KeyboardInterrupt) as exc:
            manifest["runs"][-1].update(status="interrupted", exception_type=type(exc).__name__)
            manifest["state"] = "interrupted"
            save_manifest(output, manifest)
            raise
        manifest["runs"][-1] = {**planned, **row, "status": "finished"}
        save_manifest(output, manifest)
    manifest["state"] = "finished"
    save_manifest(output, manifest)
    from reporting import summarize
    summarize(output)
    return int(any(r["exception_type"] or r["reward"] is None for r in manifest["runs"]))


if __name__ == "__main__":
    sys.exit(main())
