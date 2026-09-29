"""Compare stock warmup construction across pinned model templates on CPU."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tomllib


ROOT = Path(__file__).resolve().parent
CURRENT = ROOT.parent / "current-code"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def load_current():
    spec = importlib.util.spec_from_file_location("scope_current_builder", CURRENT / "reproduce.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def inputs(args):
    models = json.loads((ROOT / "model-pins.json").read_text())["models"]
    cases = []
    for model in models:
        family = model["family"]
        directory = args.build_root / "models" / family
        directory.mkdir(parents=True, exist_ok=True)
        for artifact in model["artifacts"]:
            name = artifact["file"]
            if name == "tokenizer.json":
                source = args.qwen_tokenizer if family == "qwen3" else args.artifacts / family / name
            else:
                source = ROOT / "templates" / family / name
            if sha(source.read_bytes()) != artifact["sha256"]:
                raise ValueError(f"Model artifact differs from pin: {family}/{name}")
            shutil.copyfile(source, directory / name)
        config = json.loads((directory / "tokenizer_config.json").read_text())
        external = None if "chat_template" in config else str(directory / "chat_template.jinja")
        modes = [("on", {"enable_thinking": True}), ("off", {"enable_thinking": False})] if family in ("qwen3", "nemotron-3-nano") else [("native-default", {})]
        for mode, settings in modes:
            kinds = ("text", "tool", "text_reasoning", "tool_reasoning") if mode == "on" else ("text", "tool")
            for kind in kinds:
                base_kind = kind.split("_")[0]
                history = [{"role": "system", "content": "Follow the user instruction."},
                           {"role": "user", "content": "Reply with exactly ready." if base_kind == "text" else "Call record_value with value ready."}]
                original = {"model": model["model"], "messages": history}
                if base_kind == "text":
                    assistant = {"role": "assistant", "content": "ready."}
                    next_message = {"role": "user", "content": "Now reply with exactly done."}
                else:
                    original["tools"] = [{"type": "function", "function": {
                        "name": "record_value", "description": "TOOL_SCHEMA_MARKER",
                        "parameters": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}}}]
                    original["tool_choice"] = "auto"
                    assistant = {"role": "assistant", "content": "", "tool_calls": [
                        {"id": "call_one", "type": "function", "function": {"name": "record_value", "arguments": '{"value": "ready"}'}}]}
                    next_message = {"role": "tool", "tool_call_id": "call_one", "content": "Recorded."}
                if kind.endswith("_reasoning"):
                    assistant["reasoning_content"] = "THOUGHT_MARKER"
                following = deepcopy(original)
                following["messages"].extend([deepcopy(assistant), next_message])
                cases.append({"id": f"{family}/{kind}/{mode}",
                              "config_path": str(directory / "tokenizer_config.json"),
                              "tokenizer_path": str(directory / "tokenizer.json"), "template_path": external,
                              "original_request": original, "completed_assistant": assistant,
                              "followup_request": following, "response_text": assistant["content"], "settings": settings})
    return cases


def comparison(prepared, following):
    if prepared["status"] != "rendered" or following["status"] != "rendered":
        return {"status": "not_comparable"}
    left, right = prepared["token_ids"], following["token_ids"]
    common = next((i for i, pair in enumerate(zip(left, right)) if pair[0] != pair[1]), min(len(left), len(right)))
    a, b = prepared["text"].encode(), following["text"].encode()
    byte_common = next((i for i, pair in enumerate(zip(a, b)) if pair[0] != pair[1]), min(len(a), len(b)))
    return {"status": "compared", "prepared_tokens": len(left), "followup_tokens": len(right),
            "common_prefix_tokens": common, "token_prefix": common == len(left),
            "common_prefix_bytes": byte_common, "text_prefix": b.startswith(a),
            "prepared_tokens_at_divergence": left[max(0, common - 3):common + 8],
            "followup_tokens_at_divergence": right[max(0, common - 3):common + 8]}


def analyze(rows, cases):
    if [r["id"] for r in rows] != [c["id"] for c in cases]:
        raise ValueError("Compiled case coverage differs from the input")
    for row in rows:
        if "setup_error" in row:
            continue
        renders = row["renders"]
        row["comparisons"] = {name: comparison(renders[name], renders["followup"]) for name in
                              ("stock", "schema_restored", "schema_and_settings_restored", "schema_settings_full_assistant")}
        row["schema_marker_present"] = {name: "TOOL_SCHEMA_MARKER" in r["text"] if r["status"] == "rendered" else None
                                        for name, r in renders.items()}
        row["ablation_changed_text"] = {}
        for effect, before, after in (("tools_only", "stock", "schema_restored"),
                                      ("settings_only", "schema_restored", "schema_and_settings_restored"),
                                      ("assistant_only", "schema_and_settings_restored", "schema_settings_full_assistant")):
            a, b = renders[before], renders[after]
            row["ablation_changed_text"][effect] = a["text"] != b["text"] if a["status"] == b["status"] == "rendered" else None
        for rendered in renders.values():
            if rendered["status"] == "rendered":
                rendered["text_sha256"] = sha(rendered["text"].encode())
        if row["stock_traits"] != {"tools": None, "settings": None, "generation_prompt": False}:
            raise ValueError("Stock defaults changed")
    return rows


def run(args):
    current = load_current()
    source_manifest = current.read_verified_sources()
    cases = inputs(args)
    input_path = args.build_root / "input.json"
    input_path.write_text(json.dumps(cases, indent=2) + "\n")
    started = datetime.now(timezone.utc).isoformat()
    result = {"schema_version": 1, "evidence": "constructed_cross_family_cpu_ablation",
              "constructed_cases_per_revision": len(cases), "model_calls": 0, "gpu_measurements": False,
              "started_at": started, "models_sha256": sha((ROOT / "model-pins.json").read_bytes()),
              "driver_sha256": sha(Path(__file__).read_bytes()), "runner_sha256": sha((ROOT / "runner.rs").read_bytes()),
              "current_driver_sha256": sha((CURRENT / "reproduce.py").read_bytes()),
              "upstream_manifest_sha256": sha((CURRENT / "sources/manifest.json").read_bytes()),
              "input_sha256": sha(input_path.read_bytes()),
              "rustc": subprocess.check_output([str(args.rustc), "--version"], text=True).strip(), "runs": [],
              "limits": ["Every message is authored. No new model response or GPU measurement is included.",
                         "The stock construction uses copied upstream fragments; ablations are diagnostic adapters, not proposed production patches.",
                         "The normal-request adapter does not exercise full frontend model selection or engine-specific preprocessing.",
                         "A rendered text prefix can fail token-prefix equality at its trailing token boundary; both comparisons are recorded.",
                         "GPT-OSS's official template embeds the current UTC date. Outputs are pinned to this run date, not guaranteed identical on another date.",
                         "Unsupported template settings are omitted. R1 and GPT-OSS are not assigned an invented thinking-off mode."]}
    env = {**os.environ, "RUSTC": str(args.rustc), "CARGO_TARGET_DIR": str(args.build_root / "target")}
    for label in ("release", "main"):
        crate, provenance = current.prepare_crate(label, args.build_root)
        source = (CURRENT / "sources" / label / "speculative_prefill.rs").read_text()
        construction, _ = current.extract(source, "    let assistant_msg =", "    let prefill_request = SpeculativePrefillRequest::new(messages);", include_end=True)
        render_line = "let formatted_prompt = formatter.render(&prefill_request)?;"
        left = source.rfind("\n", 0, source.index(render_line)) + 1
        right = source.index("\n", source.index(render_line)) + 1
        runner = (ROOT / "runner.rs").read_text().replace("// UPSTREAM_CONSTRUCTION\n", construction + "\n").replace("// UPSTREAM_RENDER\n", source[left:right])
        (crate / "src/main.rs").write_text(runner)
        command = [str(args.cargo), "build", "--manifest-path", str(crate / "Cargo.toml")]
        if args.offline:
            command.append("--offline")
        built = subprocess.run(command, env=env, text=True, capture_output=True)
        (crate / "build.log").write_text(built.stdout + built.stderr)
        if built.returncode:
            raise RuntimeError(f"{label} build failed: {crate / 'build.log'}")
        binary = crate / "scope-binary"
        shutil.copyfile(args.build_root / "target/debug/reedcode-stock-prefill-repro", binary)
        binary.chmod(0o755)
        output = subprocess.check_output([str(binary), str(input_path)])
        (crate / "output.json").write_bytes(output)
        locked = tomllib.loads((crate / "Cargo.lock").read_text())
        upstream = tomllib.loads((CURRENT / "sources" / label / "Cargo.lock").read_text())
        identity = lambda p: (p["name"], p["version"], p.get("source"), p.get("checksum"))
        expected = {identity(p) for p in upstream["package"]}
        registry = [p for p in locked["package"] if p.get("source", "").startswith("registry+")]
        if any(identity(p) not in expected for p in registry):
            raise ValueError("Registry dependency differs from upstream lock")
        result["runs"].append({"label": label, "revision": source_manifest[label + "_sha"], **provenance,
                               "registry_packages_matching_upstream": len(registry),
                               "binary_sha256": sha(binary.read_bytes()), "raw_output_sha256": sha(output),
                               "build_files_sha256": {name: sha((crate / name).read_bytes()) for name in
                                                       ("Cargo.toml", "Cargo.lock", "src/main.rs", "src/upstream-request.rs")},
                               "cases": analyze(json.loads(output), cases)})
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qwen-tokenizer", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--cargo", type=Path, required=True)
    parser.add_argument("--rustc", type=Path, required=True)
    parser.add_argument("--build-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new output path")
    args.build_root = args.build_root.resolve()
    report = run(args)
    with args.output.open("x") as output:
        output.write(json.dumps(report, indent=2) + "\n")
    for run in report["runs"]:
        print(run["label"])
        for case in run["cases"]:
            print(case["id"], case.get("setup_error") or {k: v.get("common_prefix_tokens", v["status"]) for k, v in case["comparisons"].items()})
