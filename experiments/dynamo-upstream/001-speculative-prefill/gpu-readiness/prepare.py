"""Freeze CPU-only inputs for the controlled stock/fixed builder replay."""

import argparse
from copy import deepcopy
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import time


ROOT = Path(__file__).resolve().parent
FINDING = ROOT.parent
REPO = ROOT.parents[3]
REVISION = "d72cf6dfba2660e1571ed1aa3a0d798e7ee67b31"
MODEL = "Qwen/Qwen3-8B"
READ_FILES = ["output_policy.py", "reedcode_harbor_agent.py", "model_backend.py",
              "run_experiments.py", "reporting.py", "server_metrics.py",
              "tests/test_output_policy.py", "tests/test_experiment_design.py"]
LATER_FILES = ["tests/test_harness.py", "tests/test_reporting.py"]
EXTRA_FILES = ["evals/noisy-bugfix/tests/test_pricing.py", "evals/noisy-bugfix/environment/data/test_pricing.py"]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()


def common_prefix(a, b):
    for i, (left, right) in enumerate(zip(a, b)):
        if left != right:
            return i
    return min(len(a), len(b))


def native(binary, value):
    with tempfile.NamedTemporaryFile(suffix=".json") as handle:
        handle.write(json_bytes(value))
        handle.flush()
        return subprocess.check_output([str(binary), handle.name])


def load_short():
    report = json.loads((FINDING / "session-cost/results.json").read_text())
    compressed = (FINDING / "session-cost/input.json.gz").read_bytes()
    if sha(compressed) != report["raw_evidence"]["input.json.gz"]["sha256"]:
        raise ValueError("Archived CPU fixture hash changed")
    all_inputs = json.loads(gzip.decompress(compressed))
    run_id = "noisy-bugfix_r01_on"
    cases = [deepcopy(row) for row in all_inputs["cases"] if row["name"].startswith(run_id + "/")]
    archive = REPO / "experiments/dynamo-20260928/raw-records.tar.gz"
    if sha(archive.read_bytes()) != report["archive_sha256"]:
        raise ValueError("Recorded pilot archive changed")
    with tarfile.open(archive, "r:gz") as records:
        manifest = json.load(records.extractfile("pilot/manifest.json"))
        info = next(row for row in manifest["runs"] if row["run_id"] == run_id)
        raw_trace = records.extractfile("pilot/" + info["trace"]).read()
    if sha(raw_trace) != info["trace_sha256"]:
        raise ValueError("Recorded trace differs from its manifest")
    tools = [json.loads(line) for line in raw_trace.splitlines() if json.loads(line).get("type") == "tool"]
    if len(cases) != 4 or len(tools) != 4:
        raise ValueError("Expected the complete four-continuation session")
    waits = {str(row["turn"]): row["duration_ms"] for row in tools}
    return cases, {"kind": "recorded_qwen_coding_session", "source_run": run_id,
                   "source_trace_sha256": sha(raw_trace), "source_archive_sha256": sha(archive.read_bytes()),
                   "tool_wait_ms": waits, "model_generated_assistant": True}


def assistant(command, call_id):
    return {"role": "assistant", "tool_calls": [{"id": call_id, "type": "function",
             "function": {"name": "bash", "arguments": json.dumps({"command": command})}}]}


def create_long(output, source_tools):
    sources = output / "source"
    sources.mkdir()
    hashes = {}
    for path in READ_FILES + LATER_FILES + EXTRA_FILES:
        raw = subprocess.check_output(["git", "show", f"{REVISION}:{path}"], cwd=REPO)
        target = sources / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        hashes[path] = sha(raw)
    calls = []
    commands = [["cat", *READ_FILES], ["python3", "-m", "unittest", "discover", "-s", "tests", "-p", "test_output_policy.py"],
                ["cat", *LATER_FILES]]
    for index, command in enumerate(commands):
        started = time.monotonic()
        result = subprocess.run(command, cwd=sources, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
                                env={"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1"})
        elapsed_ms = (time.monotonic() - started) * 1000
        if result.returncode:
            raise ValueError(f"Fixture tool failed: {command}")
        name = f"tool-{index}.txt"
        (output / name).write_bytes(result.stdout)
        calls.append({"argv": command, "exit_code": result.returncode, "output_file": name,
                      "output_sha256": sha(result.stdout), "duration_ms": elapsed_ms,
                      "text": "EXIT CODE: 0\n" + result.stdout.decode()})
    messages = [{"role": "system", "content": "You are a coding agent working inside a repository. Inspect files and run focused tests before proposing a change."},
                {"role": "user", "content": "Review how tool-output retention moves through ReedCode's settings, harness, experiment runner and reports. Read the implementation and relevant tests, run the focused output-policy tests, then inspect the harness and reporting tests before suggesting where a tail-only policy would need changes. Do not modify files yet."}]
    messages += [assistant(" ".join(commands[0]), "setup-read"),
                 {"role": "tool", "tool_call_id": "setup-read", "content": calls[0]["text"]}]
    request = {"model": MODEL, "messages": messages, "tools": source_tools,
               "tool_choice": "auto", "max_completion_tokens": 4096}
    cases = []
    for step in (1, 2):
        completed = assistant(" ".join(commands[step]), f"long-{step}")
        following = deepcopy(request)
        following["messages"] += [completed, {"role": "tool", "tool_call_id": f"long-{step}", "content": calls[step]["text"]}]
        cases.append({"name": f"public-repo-review/{step}", "original_request": deepcopy(request),
                      "followup_request": following, "response_text": "",
                      "effective_normal_template_args": {"enable_thinking": False}})
        request = following
    return cases, {"kind": "controlled_transcript_of_executed_public_repo_tools", "source_revision": REVISION,
                   "source_file_sha256": hashes, "model_generated_assistant": False,
                   "assistant_provenance": "Authored bash-call fixtures for the recorded CPU commands; no Qwen session was generated.",
                   "tool_wait_ms": {str(i): calls[i]["duration_ms"] for i in (1, 2)},
                   "tools": [{k: v for k, v in row.items() if k != "text"} for row in calls]}


def payload(ids):
    return {"model": MODEL, "prompt": ids, "max_tokens": 1, "temperature": 0,
            "stream": True, "stream_options": {"include_usage": True}}


def make_plan(name, cases, stock, fixed, info):
    sequence = []
    for i, case in enumerate(cases):
        old, new = stock[case["name"]], fixed[case["name"]]
        original, following = old["original"]["token_ids"], old["followup"]["token_ids"]
        if i == 0:
            sequence.append({"id": f"{name}/normal-0", "kind": "real", "case": case["name"],
                             "tokens_sha256": sha(json_bytes(original)), "payload": payload(original)})
        elif sequence[-1]["payload"]["prompt"] != original:
            raise ValueError("Replay session is not contiguous")
        before_turn = case["name"].rsplit("/", 1)[1]
        for condition, ids in (("stock", old["prepared"]["token_ids"]), ("fixed", new["prepared_ids"])):
            sequence.append({"id": f"{name}/warmup-{i + 1}-{condition}", "kind": "warmup",
                             "condition": condition, "case": case["name"],
                             "parent_id": f"{name}/normal-{i}", "tokens_sha256": sha(json_bytes(ids)),
                             "payload": payload(ids)})
        sequence.append({"id": f"{name}/normal-{i + 1}", "kind": "real", "case": case["name"],
                         "parent_id": f"{name}/normal-{i}", "recorded_tool_wait_ms": info["tool_wait_ms"][before_turn],
                         "tokens_sha256": sha(json_bytes(following)), "payload": payload(following)})
    return {"session": name, "conditions": ["off", "stock", "fixed"],
            "kind": "controlled_native_builder_token_replay", "endpoint": "/v1/completions",
            "normal_output_tokens": 1, "warmup_output_tokens": 1,
            "actual_generated_outputs_discarded": True, "captured_assistant_history_forced": True,
            "primary_arrival_rule": "After warmup completes, and no earlier than the recorded tool wait after preceding real completion.",
            "reset_required_before_each_condition_and_repeat": True, "sequence": sequence}


def prepare(args):
    args.output.mkdir(parents=True)
    short, short_info = load_short()
    source_tools = [tool for tool in short[0]["original_request"]["tools"] if tool["function"]["name"] == "bash"]
    long, long_info = create_long(args.output, source_tools)
    cases = short + long
    stock_report = json.loads((FINDING / "current-code/results.json").read_text())
    fixed_report = json.loads((FINDING / "fix/results.json").read_text())
    wanted_stock = next(row for row in stock_report["runs"] if row["label"] == "main")["binary_sha256"]
    if sha(args.stock_binary.read_bytes()) != wanted_stock or sha(args.fixed_binary.read_bytes()) != fixed_report["binary_sha256"]:
        raise ValueError("Native binary differs from its reviewed build record")
    session_report = json.loads((FINDING / "session-cost/results.json").read_text())
    if sha(args.tokenizer.read_bytes()) != session_report["tokenizer_sha256"]:
        raise ValueError("Tokenizer differs from the pinned study")
    inputs = {"config_path": str((REPO / "experiments/dynamo-prefix/fixtures/tokenizer_config.json").resolve()),
              "tokenizer_path": str(args.tokenizer.resolve()), "cases": cases}
    (args.output / "input.json.gz").write_bytes(gzip.compress(json_bytes(inputs), mtime=0))
    old_raw, fixed_raw = native(args.stock_binary, inputs), native(args.fixed_binary, inputs)
    old = {row["case"]: row for row in json.loads(old_raw)}
    fixed = {row["case"]: row for row in json.loads(fixed_raw)}
    (args.output / "stock-native.json.gz").write_bytes(gzip.compress(old_raw, mtime=0))
    (args.output / "fixed-native.json.gz").write_bytes(gzip.compress(fixed_raw, mtime=0))
    checks = []
    for case in cases:
        stock, patched = old[case["name"]], fixed[case["name"]]
        original, following = stock["original"]["token_ids"], stock["followup"]["token_ids"]
        if patched["original_ids"] != original or patched["followup_ids"] != following:
            raise ValueError("Ordinary request tokens changed")
        prefix = patched["prepared_ids"]
        if not prefix or following[:len(prefix)] != prefix:
            raise ValueError("Fixed prefix failed")
        if max(len(original), len(following), len(prefix), len(stock["prepared"]["token_ids"])) + 1 > 32768:
            raise ValueError("Payload exceeds the 32768-token context")
        if case in long and not 16000 <= len(original) <= 24000:
            raise ValueError("Long original input is outside the approved target range")
        checks.append({"case": case["name"], "original_tokens": len(original), "followup_tokens": len(following),
                       "stock_warmup_tokens": len(stock["prepared"]["token_ids"]),
                       "stock_first_difference": common_prefix(stock["prepared"]["token_ids"], following),
                       "fixed_warmup_tokens": len(prefix), "fixed_exact_prefix": True,
                       "normal_tokens_unchanged": True})
    for name, chosen, info in (("short", short, short_info), ("long", long, long_info)):
        plan = make_plan(name, chosen, old, fixed, info)
        (args.output / f"{name}-replay.json.gz").write_bytes(gzip.compress(json_bytes(plan), mtime=0))
    order = ["off", "stock", "fixed"]
    schedule = [{"repeat": repeat + 1, "session": name, "condition": condition,
                 "reset_before": True} for repeat in range(3) for name in ("short", "long")
                for condition in order[repeat:] + order[:repeat]]
    (args.output / "schedule.json").write_bytes(json_bytes(schedule))
    manifest = {"status": "CPU fixtures validated; real backend replay not yet exercised", "gpu_measurements": False,
                "model_requests_made": 0, "source_revision": fixed_report["source_revision"],
                "model": MODEL, "model_revision": "b968826d9c46dd6066d109eabc6255188de91218",
                "stock_binary_sha256": wanted_stock, "fixed_binary_sha256": fixed_report["binary_sha256"],
                "patch_sha256": {name: sha((FINDING / "fix" / name).read_bytes()) for name in ("dynamo.patch", "renderer.patch")},
                "sessions": {"short": short_info, "long": long_info}, "checks": checks,
                "controlled_replay_limits": ["Normal and warmup requests generate one discarded token; original sampled output is not regenerated.",
                    "Recorded assistant history is held fixed independently of newly sampled output.",
                    "Missing original decode KV can overstate added fixed-warmup reuse relative to a live agent trajectory.",
                    "Native helpers invoke the builders during preparation; HTTP replay sends their frozen exact token IDs.",
                    "Token-input completion transport, backend capture, cache reset and event collection require live smoke checks.",
                    "The generated end-to-end hint check remains separate from this controlled replay."],
                "files": {str(path.relative_to(args.output)): sha(path.read_bytes()) for path in sorted(args.output.rglob("*")) if path.is_file()}}
    (args.output / "manifest.json").write_bytes(json_bytes(manifest))
    print(json.dumps(checks, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--stock-binary", type=Path, default=Path("/tmp/reedcode-current-stock-prefill/main/reproduction-binary"))
    parser.add_argument("--fixed-binary", type=Path, default=Path("/tmp/reedcode-current-stock-prefill/target/debug/reedcode-prefill-fix-check"))
    parser.add_argument("--tokenizer", type=Path, default=Path("/tmp/reedcode-prefix-tokenizer/tokenizer.json"))
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new output directory")
    prepare(args)
