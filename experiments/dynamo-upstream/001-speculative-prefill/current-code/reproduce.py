"""Compile pinned upstream warmup code and replay two archived requests on CPU."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib


ROOT = Path(__file__).resolve().parent
PREFIX = ROOT.parents[2] / "dynamo-prefix"
sys.path.insert(0, str(PREFIX))
import audit
import survey


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_verified_sources():
    manifest = json.loads((ROOT / "sources/manifest.json").read_text())
    for item in manifest["files"]:
        data = (ROOT / "sources" / item["local_path"]).read_bytes()
        if sha(data) != item["sha256"]:
            raise ValueError(f"Pinned upstream source changed: {item['local_path']}")
    return manifest


def extract(source, start, end, *, include_end=False):
    if source.count(start) != 1:
        raise ValueError(f"Ambiguous source anchor: {start}")
    left = source.index(start)
    right = source.index(end, left)
    if include_end:
        right += len(end)
    block = source[left:right]
    return block, {"first_line": source[:left].count("\n") + 1,
                   "last_line": source[:right].count("\n") + (not block.endswith("\n")),
                   "sha256": sha(block.encode())}


def prepare_crate(label, build_root):
    source = (ROOT / "sources" / label / "speculative_prefill.rs").read_text()
    preprocessor = (ROOT / "sources" / label / "preprocessor.rs").read_text()
    invocation, invocation_info = extract(preprocessor,
        "        let final_stream = speculative_prefill::maybe_wrap_stream(", "        );", include_end=True)
    request, request_info = extract(source, "pub struct SpeculativePrefillRequest", "/// Optionally wraps")
    construction, construction_info = extract(source, "    let assistant_msg =", "    let prefill_request = SpeculativePrefillRequest::new(messages);", include_end=True)
    render_line = "let formatted_prompt = formatter.render(&prefill_request)?;"
    if source.count(render_line) != 1:
        raise ValueError("Unexpected upstream render call")
    line_start = source.rfind("\n", 0, source.index(render_line)) + 1
    line_end = source.index("\n", source.index(render_line)) + 1
    render = source[line_start:line_end]
    render_info = {"first_line": source[:line_start].count("\n") + 1,
                   "last_line": source[:line_end].count("\n"), "sha256": sha(render.encode())}
    production = source.split("#[cfg(test)]", 1)[0]
    gates = {
        "text_delta_accumulator_present": "choice.delta.content" in production and ".push_str(text)" in production,
        "finish_reason_trigger_present": "choice.finish_reason.is_some()" in production,
        "original_messages_copied": "request.inner.messages.clone()" in production,
        "stock_hint_checked": "speculative_prefill" in production and ".agent_hints" in production,
        "request_has_no_tools_method": "fn tools(" not in request,
        "request_has_no_template_args_method": "fn chat_template_args(" not in request,
        "stream_does_not_accumulate_tool_calls": "choice.delta.tool_calls" not in production,
        "preprocessor_calls_wrapper_with_request_and_formatter": "&request," in invocation and "&self.formatter," in invocation,
    }
    if not all(gates.values()):
        raise ValueError(f"Upstream source no longer matches the isolated path: {gates}")
    upstream_lock = tomllib.loads((ROOT / "sources" / label / "Cargo.lock").read_text())
    wanted = ("dynamo-renderer", "dynamo-protocols", "dynamo-tokenizers", "minijinja", "serde_json", "serde", "anyhow")
    packages = {}
    for name in wanted:
        found = [p for p in upstream_lock["package"] if p["name"] == name]
        if len(found) != 1:
            raise ValueError(f"Ambiguous pinned dependency {name}")
        packages[name] = found[0]
    manifest = ('[package]\nname = "reedcode-stock-prefill-repro"\nversion = "0.1.0"\nedition = "2024"\npublish = false\n\n[dependencies]\n')
    for name in wanted:
        version = packages[name]["version"]
        features = {"minijinja": '["preserve_order"]', "serde_json": '["preserve_order"]', "serde": '["derive"]'}.get(name)
        manifest += (f'{name} = {{ version = "={version}", features = {features} }}\n' if features
                     else f'{name} = "={version}"\n')
    manifest += '\n[workspace]\n'
    crate = build_root / label
    (crate / "src").mkdir(parents=True, exist_ok=True)
    (crate / "Cargo.toml").write_text(manifest)
    (crate / "src/upstream-request.rs").write_text(request)
    runner = (ROOT / "runner.rs").read_text().replace("// UPSTREAM_CONSTRUCTION\n", construction + "\n").replace("// UPSTREAM_RENDER\n", render)
    (crate / "src/main.rs").write_text(runner)
    if not (crate / "Cargo.lock").exists():
        shutil.copyfile(ROOT / "sources" / label / "Cargo.lock", crate / "Cargo.lock")
    return crate, {"source_sha256": sha(source.encode()), "extracted_blocks": {
        "request": request_info, "assistant_construction": construction_info, "render_call": render_info},
        "preprocessor_invocation": {"source_sha256": sha(preprocessor.encode()), **invocation_info},
        "source_gates": gates, "pinned_packages": packages}


def archived_input(tokenizer, model_dir):
    # This verifies the existing capture and its extracted runtime evidence first.
    spec = importlib.util.spec_from_file_location("stock_archived_reproduction", ROOT.parent / "reproduce.py")
    prior = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prior)
    old = prior.reproduce(tokenizer)
    records = survey.read_records(survey.ARCHIVE)
    model_dir.mkdir(parents=True, exist_ok=True)
    if sha(tokenizer.read_bytes()) != audit.TOKENIZER_SHA256:
        raise ValueError("Tokenizer differs from the archived pin")
    config = PREFIX / "fixtures/tokenizer_config.json"
    if sha(config.read_bytes()) != audit.CONFIG_SHA256:
        raise ValueError("Config differs from the archived pin")
    shutil.copyfile(tokenizer, model_dir / "tokenizer.json")
    shutil.copyfile(config, model_dir / "tokenizer_config.json")
    cases = []
    for name in ("text", "tool"):
        rows = json.loads(records[f"prefix-check/{name}-on.json"])
        assistant = rows[0]["response"]["choices"][0]["message"]
        audit.check_followup_contract(rows[0]["request"], assistant, rows[1]["request"])
        cases.append({"name": name, "original_request": rows[0]["request"],
                      "followup_request": rows[1]["request"],
                      "response_text": assistant.get("content") or "",
                      "effective_normal_template_args": {"enable_thinking": False}})
    return {"config_path": str((model_dir / "tokenizer_config.json").resolve()),
            "tokenizer_path": str((model_dir / "tokenizer.json").resolve()), "cases": cases}, old, records


def verify_runtime(rows, records, prior, *, require_archived_normal):
    prior_cases = {r["case"]: r for r in prior["cases"]}
    events = [json.loads(line)["event"] for line in records["server/frontend-trace.jsonl"].decode().splitlines()]
    ends = {e["request"]["request_id"]: e for e in events if e["event_type"] == "request_end"}
    logs = [json.loads(line) for line in records["server/frontend.log"].decode().splitlines() if line.startswith("{")]
    routes = [r for r in logs if r.get("message") == "[ROUTING_INPUT] request local hashes"]
    spec = importlib.util.spec_from_file_location("stock_routing_check", ROOT.parent / "reproduce.py")
    reconstruction = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reconstruction)
    expected_order = ["text", "tool"]
    if [r["case"] for r in rows] != expected_order:
        raise ValueError("Compiled fixture coverage changed")
    for row in rows:
        old = prior_cases[row["case"]]
        first_id, next_id = old["original_request_id"], old["followup_request_id"]
        original = row["original"]["token_ids"]
        prepared = row["prepared"]["token_ids"]
        following = row["followup"]["token_ids"]
        normal_checks = {"original": audit.validate_server_prompt(original, ends[first_id]),
                         "followup": audit.validate_server_prompt(following, ends[next_id])}
        if require_archived_normal and any(c["status"] != "verified" for c in normal_checks.values()):
            raise ValueError("Release normal-request adapter differs from archived runtime")
        first_at = next(i for i, r in enumerate(routes) if r.get("request_id") == first_id)
        next_at = next(i for i, r in enumerate(routes) if r.get("request_id") == next_id)
        between = routes[first_at + 1:next_at]
        if len(between) != 1 or between[0].get("request_id"):
            raise ValueError("Archived internal warmup no longer isolated")
        try:
            warmup = {"status": "verified", **reconstruction.check_routing(prepared, between[0])}
        except ValueError:
            warmup = {"status": "different_from_archived_runtime", "archived_tokens": between[0]["isl_tokens"]}
        divergence = audit.common_prefix(prepared, following)
        row["comparison"] = {"first_different_token_index": divergence,
                             "prepared_is_prefix": divergence == len(prepared),
                             "prepared_tokens": len(prepared), "followup_tokens": len(following),
                             "normal_runtime_hash_checks": normal_checks,
                             "warmup_runtime_hash_check": warmup,
                             "prior_reconstruction_divergence": old["exact_divergence_from_cpu_reconstruction"]}
        for key in ("original", "prepared", "followup"):
            row[key]["text_sha256"] = sha(row[key]["text"].encode())
            row[key]["token_ids_json_sha256"] = sha(json.dumps(row[key]["token_ids"], separators=(",", ":")).encode())
        traits = row["stock_request_traits"]
        if traits["tools"] is not None or traits["chat_template_args"] is not None or traits["add_generation_prompt"]:
            raise ValueError("Stock trait defaults were changed")
    return rows


def run(args):
    manifest = read_verified_sources()
    input_data, prior, records = archived_input(args.tokenizer, args.build_root / "model")
    input_path = args.build_root / "input.json"
    input_path.write_text(json.dumps(input_data, indent=2) + "\n")
    result = {"schema_version": 1, "evidence": "compiled_unchanged_upstream_fragments_with_matching_rust_renderer_and_tokenizer",
              "new_gpu_requests": 0, "archive_sha256": prior["archive_sha256"],
              "source_manifest_sha256": sha((ROOT / "sources/manifest.json").read_bytes()),
              "runner_sha256": sha((ROOT / "runner.rs").read_bytes()),
              "driver_sha256": sha(Path(__file__).read_bytes()),
              "input_sha256": sha(input_path.read_bytes()), "tokenizer_sha256": audit.TOKENIZER_SHA256,
              "config_sha256": audit.CONFIG_SHA256, "template_sha256": audit.TEMPLATE_SHA256,
              "rustc": subprocess.check_output([str(args.rustc), "--version"], text=True).strip(),
              "cargo": subprocess.check_output([str(args.cargo), "--version"], text=True).strip(),
              "runs": [],
              "limits": ["Only the copied request implementation and assistant construction/render call are compiled from Dynamo.",
                         "The normal-request adapter supplies recorded effective thinking settings; full preprocessor transforms are not executed.",
                         "Response text comes from captured completed single-choice responses; streaming accumulation is source-checked, not executed.",
                         "Async cancellation, task admission, routing, server execution and GPU cache persistence are untested here.",
                         "Archived runtime hashes are from 1.5.0 only. Matching them is a sanity check, not a current-main serving result."]}
    env = {**os.environ, "RUSTC": str(args.rustc), "CARGO_TARGET_DIR": str((args.build_root / "target").resolve())}
    for label in ("release", "main"):
        crate, provenance = prepare_crate(label, args.build_root)
        command = [str(args.cargo), "build", "--manifest-path", str(crate / "Cargo.toml")]
        if args.offline:
            command.append("--offline")
        built = subprocess.run(command, env=env, text=True, capture_output=True)
        (crate / "build.log").write_text(built.stdout + built.stderr)
        if built.returncode:
            raise RuntimeError(f"{label} build failed; inspect {crate / 'build.log'}")
        binary = args.build_root / "target/debug/reedcode-stock-prefill-repro"
        binary_copy = crate / "reproduction-binary"
        shutil.copyfile(binary, binary_copy)
        binary_copy.chmod(0o755)
        output = subprocess.check_output([str(binary_copy), str(input_path)])
        (crate / "output.json").write_bytes(output)
        locked = tomllib.loads((crate / "Cargo.lock").read_text())
        actual = {p["name"]: p for p in locked["package"] if p["name"] in provenance["pinned_packages"]}
        if any(actual[name]["version"] != pin["version"] or actual[name].get("checksum") != pin.get("checksum")
               for name, pin in provenance["pinned_packages"].items()):
            raise ValueError("Compiled dependency differs from upstream lock")
        upstream_lock = tomllib.loads((ROOT / "sources" / label / "Cargo.lock").read_text())
        identity = lambda p: (p["name"], p["version"], p.get("source"), p.get("checksum"))
        upstream_registry = {identity(p) for p in upstream_lock["package"] if p.get("source", "").startswith("registry+")}
        registry = [p for p in locked["package"] if p.get("source", "").startswith("registry+")]
        drift = [dict(zip(("name", "version", "source", "checksum"), identity(p))) for p in registry
                 if identity(p) not in upstream_registry]
        result["runs"].append({"label": label, "revision": manifest[label + "_sha"], **provenance,
                               "binary_sha256": sha(binary_copy.read_bytes()), "output_sha256": sha(output),
                               "build_command": command,
                               "registry_dependency_audit": {"packages": len(registry), "differences_from_upstream_lock": drift},
                               "build_files_sha256": {p: sha((crate / p).read_bytes()) for p in
                                    ("Cargo.toml", "Cargo.lock", "src/main.rs", "src/upstream-request.rs")},
                               "cases": verify_runtime(json.loads(output), records, prior,
                                                       require_archived_normal=label == "release")})
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tokenizer", type=Path, required=True)
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
    print(json.dumps([{r["label"]: [c["comparison"] for c in r["cases"]]} for r in report["runs"]], indent=2))
