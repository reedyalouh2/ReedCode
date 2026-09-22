"""Run both output caps and save a manifest of the results."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
REAL_TASKS = ("regex-log", "extract-elf", "sanitize-git-repo")


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
    args = parser.parse_args()
    if not os.getenv("OPENAI_API_KEY"):
        parser.error("Set OPENAI_API_KEY in the environment before running model evaluations")
    if not shutil.which("harbor"):
        parser.error("harbor is not on PATH; run with uv run python run_experiments.py ...")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = (args.output or ROOT / "runs" / f"{args.suite}-{stamp}").resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": 1,
        "model": args.model,
        "protocol": "sequential: all 20K trials, then all 2K trials",
        "harness_sha256": hashlib.sha256((ROOT / "reedcode_harbor_agent.py").read_bytes()).hexdigest(),
        "runs": [],
    }
    import importlib.metadata
    manifest["harbor_version"] = importlib.metadata.version("harbor")
    manifest["openai_version"] = importlib.metadata.version("openai")
    tasks = REAL_TASKS if args.suite == "real" else ("1", "2", "3")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    # Use the same turn limit and timeout in both conditions.
    env.update(MAX_TURNS="30", TOOL_TIMEOUT="120")
    for cap in (20000, 2000):
        for task in tasks:
            run_id = f"{cap}_{task}"
            job_name = f"reedcode-{args.suite}-{stamp}-{run_id}"
            selector = ["-t", f"terminal-bench/{task}"] if args.suite == "real" else ["-p", "evals/noisy-bugfix"]
            command = ["harbor", "run", *selector, "--agent", "reedcode_harbor_agent:ReedCodeAgent",
                       "--model", args.model, "--job-name", job_name, "--jobs-dir", str(ROOT / "jobs")]
            print(f"Running {run_id}; output: {output}", flush=True)
            result = subprocess.run(command, cwd=ROOT, env={**env, "MAX_TOOL_OUTPUT": str(cap)})
            row = export_run(ROOT / "jobs" / job_name, output, run_id,
                             task if args.suite == "real" else "noisy-bugfix", cap, result.returncode)
            manifest["runs"].append(row)
            (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    from reporting import summarize
    summarize(output)
    return int(any(r["exception_type"] or r["reward"] is None for r in manifest["runs"]))


if __name__ == "__main__":
    sys.exit(main())
