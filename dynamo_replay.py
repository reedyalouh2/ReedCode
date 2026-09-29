"""Replay fixed Chat requests through Dynamo, with tool waits between turns."""

import argparse
import asyncio
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import time
from uuid import uuid4

from openai import AsyncOpenAI

from chat_stream import stream_completion
from dynamo_metrics import EpochMetrics
from dynamo_support import DynamoConfig, finish_session
from run_dynamo import build_plan
from run_experiments import ROOT, save_manifest


REQUEST_FIELDS = {"messages", "tools", "tool_choice", "max_completion_tokens", "temperature",
                  "top_p", "seed", "stop", "parallel_tool_calls"}


def validate_workload(workload):
    workflows = workload.get("workflows", [])
    if workload.get("schema_version") != 1 or not workflows:
        raise ValueError("Expected schema_version 1 and at least one workflow")
    ids = [w.get("id") for w in workflows]
    if any(not isinstance(x, str) or not x for x in ids) or len(set(ids)) != len(ids):
        raise ValueError("Workflow IDs must be unique nonempty strings")
    for workflow in workflows:
        if not workflow.get("turns"):
            raise ValueError("Each workflow needs at least one turn")
        for value in [workflow.get("arrival_ms", 0), *(t.get("wait_ms", 0) for t in workflow["turns"])]:
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("Arrival and wait times must be finite and nonnegative")
        for turn in workflow["turns"]:
            request = turn["request"]
            if set(request) - REQUEST_FIELDS or not isinstance(request.get("messages"), list) or not request["messages"]:
                raise ValueError("Unsupported replay request fields or missing messages")
            if type(request.get("max_completion_tokens")) is not int or request["max_completion_tokens"] < 1:
                raise ValueError("Each request needs a positive max_completion_tokens budget")
    return workload


def capture_workload(trace):
    events = [json.loads(line) for line in Path(trace).read_text().splitlines() if line.strip()]
    configs = [e for e in events if e["type"] == "run_config"]
    if len(configs) != 1 or configs[0].get("capture_requests") is not True:
        raise ValueError("Use a single trace recorded with DYNAMO_CAPTURE_REQUESTS=1")
    turns = []
    for event in events:
        if event["type"] != "request":
            continue
        number = event["turn"]
        responses = [e for e in events if e["type"] == "assistant_message" and e["turn"] == number]
        inference = [e for e in events if e["type"] == "inference" and e["turn"] == number]
        if len(responses) != 1 or len(inference) != 1 or inference[0].get("response_status") != "completed":
            raise ValueError("Capture has an incomplete turn; keep it as failure evidence, not a replay fixture")
        request = {key: value for key, value in event["request"].items() if key != "model"}
        wait_ms = sum(e["duration_ms"] for e in events if e["type"] == "tool" and e["turn"] == number - 1)
        turns.append({"request": request, "wait_ms": wait_ms, "expected_message": responses[0]["message"]})
    summary = [e for e in events if e["type"] == "task_summary"]
    if not summary or summary[-1].get("stop_reason") != "no_tool_calls":
        raise ValueError("Capture must finish normally before it can become a replay fixture")
    return validate_workload({"schema_version": 1, "source_model": configs[0]["model"],
                              "source_trace_sha256": hashlib.sha256(Path(trace).read_bytes()).hexdigest(),
                              "workflows": [{"id": "captured", "arrival_ms": 0, "turns": turns}]})


def comparable_message(message):
    return {key: value for key, value in message.items()
            if key in {"role", "content", "tool_calls", "reasoning", "reasoning_content"}
            and value is not None}


async def run_epoch(client, *, workload, model, condition, copies, horizon_s, output, metrics=None):
    epoch_id = uuid4().hex
    started = time.perf_counter()
    rows = []
    trace = output / "requests.jsonl"

    def log(event):
        event = {"elapsed_ms": (time.perf_counter() - started) * 1000, **event}
        with trace.open("a") as file:
            file.write(json.dumps(event) + "\n")

    async def workflow_run(workflow, copy_number):
        session = f"replay-{epoch_id}-{copy_number}-{uuid4().hex}"
        arrival = started + workflow.get("arrival_ms", 0) / 1000
        await asyncio.sleep(max(0, arrival - time.perf_counter()))
        row = {"workflow": workflow["id"], "copy": copy_number, "session_id": session,
               "status": "running", "calls": [], "capped_completion_ms": horizon_s * 1000}
        rows.append(row)
        try:
            async with asyncio.timeout(max(0, arrival + horizon_s - time.perf_counter())):
                for turn_number, turn in enumerate(workflow["turns"], 1):
                    await asyncio.sleep(turn.get("wait_ms", 0) / 1000)
                    request = copy.deepcopy(turn["request"])
                    request_hash = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
                    event = {"type": "request", "session_id": session, "turn": turn_number,
                             "request_sha256": request_hash, "status": "running"}
                    row["calls"].append(event)
                    log(event)
                    try:
                        completion, timing = await stream_completion(
                            client, model=model, **request,
                            **DynamoConfig(condition == "on").request_options(session))
                    except BaseException as exc:
                        event.update(status="failed", error_type=type(exc).__name__,
                                     stream_timing=getattr(exc, "stream_timing", None))
                        log({**event, "type": "response"})
                        raise
                    choice = completion.choices[0]
                    actual = comparable_message(choice.message.model_dump(exclude_none=True))
                    expected = turn.get("expected_message")
                    event.update(status="completed", completion_id=completion.id,
                                 served_model=completion.model, stream_timing=timing,
                                 finish_reason=choice.finish_reason,
                                 usage=completion.usage.model_dump() if completion.usage else None,
                                 recorded_message_match=(actual == comparable_message(expected))
                                 if expected is not None else None)
                    log({**event, "type": "response"})
                    if choice.finish_reason not in ("stop", "tool_calls"):
                        row["status"] = "output_limit" if choice.finish_reason == "length" else "incomplete"
                        return
                row.update(status="completed", capped_completion_ms=(time.perf_counter() - arrival) * 1000)
        except TimeoutError:
            row["status"] = "timeout"
        except asyncio.CancelledError:
            row["status"] = "cancelled"
            raise
        except Exception as exc:
            row.update(status="error", error_type=type(exc).__name__)
        finally:
            row["session_final"] = await finish_session(client, model, session)
            row["elapsed_including_cleanup_ms"] = (time.perf_counter() - arrival) * 1000
            log({"type": "workflow_end", **row})

    collector = metrics or EpochMetrics(None)
    completed = False
    try:
        async with collector:
            async with asyncio.TaskGroup() as group:
                for number in range(copies):
                    for workflow in workload["workflows"]:
                        group.create_task(workflow_run(workflow, number))
        completed = True
    finally:
        result = {"epoch_id": epoch_id, "condition": condition, "workflows": rows,
                  "status": "finished" if completed else "interrupted",
                  "elapsed_s": time.perf_counter() - started, "metrics": collector.result}
        (output / "epoch.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


async def execute(args, workload, output, manifest, client=None):
    owned_client = client is None
    client = client or AsyncOpenAI(base_url=args.base_url, api_key=os.environ["OPENAI_API_KEY"],
                                   max_retries=0, timeout=args.horizon_seconds)
    manifest["state"] = "running"
    try:
        for planned in manifest["plan"]:
            directory = output / planned["run_id"]
            directory.mkdir()
            row = {**planned, "status": "running", "directory": directory.name}
            manifest["epochs"].append(row)
            save_manifest(output, manifest)
            metrics = EpochMetrics(args.metrics_url, model_name=args.model,
                                   sample_interval=args.sample_interval,
                                   identity_url=args.metrics_identity_url)
            result = await run_epoch(client, workload=workload, model=args.model,
                                     condition=planned["condition"], copies=args.copies,
                                     horizon_s=args.horizon_seconds, output=directory, metrics=metrics)
            row.update(status="finished", sha256=hashlib.sha256((directory / "epoch.json").read_bytes()).hexdigest())
            save_manifest(output, manifest)
        manifest["state"] = "finished"
    except BaseException:
        manifest["state"] = "interrupted"
        if manifest["epochs"] and manifest["epochs"][-1]["status"] == "running":
            manifest["epochs"][-1]["status"] = "interrupted"
        raise
    finally:
        save_manifest(output, manifest)
        if owned_client:
            await client.close()
        from dynamo_report import report_replay
        report_replay(output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    capture = sub.add_parser("capture")
    capture.add_argument("trace", type=Path)
    capture.add_argument("--output", required=True, type=Path)
    run = sub.add_parser("run")
    run.add_argument("workload", type=Path)
    run.add_argument("--model", required=True)
    run.add_argument("--output", type=Path)
    run.add_argument("--base-url")
    run.add_argument("--deployment", type=Path)
    run.add_argument("--metrics-url")
    run.add_argument("--metrics-identity-url", help="Live process identity for this metrics endpoint")
    run.add_argument("--sample-interval", type=float, default=0.1)
    run.add_argument("--horizon-seconds", type=float, default=120)
    run.add_argument("--copies", type=int, default=1)
    run.add_argument("--repeats", type=int, default=5)
    run.add_argument("--seed", type=int, default=20260927)
    run.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "capture":
        data = capture_workload(args.trace)
        with args.output.open("x") as file:
            file.write(json.dumps(data, indent=2) + "\n")
        return 0
    if (args.copies < 1 or not math.isfinite(args.horizon_seconds) or args.horizon_seconds <= 0
            or not math.isfinite(args.sample_interval) or args.sample_interval <= 0):
        parser.error("Copies, horizon, and sampling interval must be positive and finite")
    if args.metrics_identity_url and not args.metrics_url:
        parser.error("--metrics-identity-url requires --metrics-url")
    workload = validate_workload(json.loads(args.workload.read_text()))
    if workload.get("source_model") not in (None, args.model):
        parser.error("The replay model must match the capture's source_model")
    plan = [{k: p[k] for k in ("run_id", "repeat", "position", "condition")}
            for p in build_plan(args.repeats, args.seed)]
    deployment = None
    if args.execute:
        from run_dynamo import read_deployment, validate_url
        if not args.base_url or not args.deployment or not os.getenv("OPENAI_API_KEY"):
            parser.error("--execute requires --base-url, --deployment, and OPENAI_API_KEY")
        validate_url(args.base_url)
        if args.metrics_url:
            validate_url(args.metrics_url)
        if args.metrics_identity_url:
            validate_url(args.metrics_identity_url)
        deployment = read_deployment(args.deployment)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = (args.output or ROOT / "runs" / f"dynamo-replay-{stamp}").resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "workload.json").write_text(json.dumps(workload, indent=2) + "\n")
    sources = ("dynamo_replay.py", "chat_stream.py", "dynamo_support.py", "dynamo_metrics.py",
               "server_metrics.py", "metrics_identity.py", "dynamo_report.py", "run_dynamo.py", "reporting.py", "uv.lock")
    manifest = {"study_kind": "dynamo_replay", "schema_version": 1, "state": "planned",
                "environment": "unverified_server", "model": args.model, "copies": args.copies,
                "schedule_seed": args.seed, "repeats": args.repeats,
                "horizon_s": args.horizon_seconds, "plan": plan, "epochs": [],
                "workload_sha256": hashlib.sha256((output / "workload.json").read_bytes()).hexdigest(),
                "source_sha256": {s: hashlib.sha256((ROOT / s).read_bytes()).hexdigest() for s in sources},
                "deployment": deployment, "cache_policy": "carry_between_epochs",
                "metrics_enabled": bool(args.metrics_url), "sample_interval_s": args.sample_interval,
                "restart_identity_source": "process_sidecar" if args.metrics_identity_url else "prometheus",
                "hint_application": "unverified"}
    save_manifest(output, manifest)
    print(f"{len(plan)} epochs, {args.copies * len(workload['workflows'])} workflows per epoch: {output}")
    if args.execute:
        asyncio.run(execute(args, workload, output, manifest))
    else:
        print("Plan only. No model or server requests.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
