"""Compare the pinned Rust formatter with explicit Python template fixtures."""

import argparse
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile

from jinja2 import Environment
from tokenizers import Tokenizer


ROOT = Path(__file__).resolve().parent
PREFIX = ROOT.parents[1] / "dynamo-prefix"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha(data):
    return hashlib.sha256(data).hexdigest()


def collect(artifacts, qwen_tokenizer):
    audit = load("renderer_parity_audit", PREFIX / "audit.py")
    families = load("renderer_parity_families", PREFIX / "family_templates.py")
    families.verify(artifacts)
    audit.QwenRenderer(qwen_tokenizer)
    evidence = json.loads((PREFIX / "family-template-evidence.json").read_text())
    sources = [{"family": "qwen3", "model": audit.MODEL, "revision": audit.REVISION,
                "config": PREFIX / "fixtures/tokenizer_config.json", "tokenizer": qwen_tokenizer}]
    sources.extend({"family": m["family"], "model": m["model"], "revision": m["revision"],
                    "config": artifacts / m["family"] / "tokenizer_config.json",
                    "tokenizer": artifacts / m["family"] / "tokenizer.json"} for m in evidence["models"])
    cases, references = [], {}
    for source in sources:
        family = source["family"]
        config = json.loads(source["config"].read_text())
        tokenizer = Tokenizer.from_file(str(source["tokenizer"]))
        environment = Environment(trim_blocks=True, lstrip_blocks=True)
        environment.filters["tojson"] = lambda value: json.dumps(value, ensure_ascii=False)
        template = environment.from_string(config["chat_template"])
        special = {key: value["content"] if isinstance(value, dict) else value
                   for key, value in config.items() if key in ("bos_token", "eos_token", "pad_token", "unk_token")}
        basic_cases = families.fixtures("deepseek-r1" if family == "qwen3" else family)
        reference_cases = [(name, messages, settings, None) for name, messages, settings in basic_cases]
        tools = [{"type": "function", "function": {"name": "read_file", "description": "Read a file.",
                  "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                                 "required": ["path"]}}}]
        reference_cases.extend((name + "-with-schema", messages, settings, tools)
                               for name, messages, settings in basic_cases
                               if name in ("tool-current", "tool-next-user"))
        supports_thinking_toggle = family != "deepseek-r1"
        if supports_thinking_toggle:
            reference_cases = [(name + ("-thinking-off" if not thinking else ""), messages,
                                {"enable_thinking": thinking, **settings}, definitions)
                               for name, messages, settings, definitions in reference_cases
                               for thinking in (True, False)]
        for name, messages, settings, definitions in reference_cases:
            ident = family + "/" + name
            wire = deepcopy(messages)
            serialized_arguments = False
            for message in wire:
                for call in message.get("tool_calls", []):
                    if not isinstance(call["function"]["arguments"], str):
                        call["function"]["arguments"] = json.dumps(call["function"]["arguments"], ensure_ascii=False)
                        serialized_arguments = True
            request = {"model": source["model"], "messages": wire}
            if definitions is not None:
                request["tools"] = definitions
            cases.append({"id": ident, "config_path": str(source["config"].resolve()),
                          "request": request, "template_args": settings})
            template_inputs = {"tools": definitions} if definitions is not None else {}
            text = template.render(messages=messages, add_generation_prompt=True,
                                   **template_inputs, **special, **settings)
            references[ident] = {"text": text, "tokenizer": tokenizer,
                                  "template_args": settings,
                                  "supports_thinking_toggle": supports_thinking_toggle,
                                  "reference_arguments_serialized_for_wire": serialized_arguments,
                                  "source": {key: str(value) for key, value in source.items()},
                                  "config_sha256": sha(source["config"].read_bytes()),
                                  "tokenizer_sha256": sha(source["tokenizer"].read_bytes())}
    return cases, references


def compare(binary, artifacts, qwen_tokenizer):
    cases, references = collect(artifacts, qwen_tokenizer)
    with tempfile.TemporaryDirectory(prefix="reedcode-rust-renderer-") as directory:
        path = Path(directory) / "cases.json"
        path.write_text(json.dumps(cases, ensure_ascii=False))
        completed = subprocess.run([str(binary.resolve()), str(path)], check=True, capture_output=True, text=True)
    rust = json.loads(completed.stdout)
    if [row["id"] for row in rust] != [row["id"] for row in cases]:
        raise ValueError("Rust formatter coverage differs from submitted cases")
    rows = []
    for row in rust:
        reference = references[row["id"]]
        expected = reference["text"]
        tokenizer = reference["tokenizer"]
        expected_ids = tokenizer.encode(expected, add_special_tokens=False).ids
        result = {"id": row["id"], "rust_status": row["status"],
                  "template_args": reference["template_args"],
                  "supports_thinking_toggle": reference["supports_thinking_toggle"],
                  "source": reference["source"], "config_sha256": reference["config_sha256"],
                  "tokenizer_sha256": reference["tokenizer_sha256"],
                  "reference_arguments_serialized_for_wire": reference["reference_arguments_serialized_for_wire"],
                  "reference_text_sha256": sha(expected.encode()), "reference_tokens": len(expected_ids),
                  "reference_thought_retained": "THOUGHT_MARKER" in expected}
        if row["status"] == "rendered":
            actual_ids = tokenizer.encode(row["text"], add_special_tokens=False).ids
            shared = 0
            for left, right in zip(expected_ids, actual_ids):
                if left != right:
                    break
                shared += 1
            result.update(text_equal=row["text"] == expected, token_ids_equal=actual_ids == expected_ids,
                          rust_text_sha256=sha(row["text"].encode()), rust_tokens=len(actual_ids),
                          shared_prefix_tokens=shared, rust_thought_retained="THOUGHT_MARKER" in row["text"],
                          rust_text=row["text"], reference_text=expected)
        else:
            result["error"] = row["error"]
        rows.append(result)
    return {"schema_version": 1, "evidence": "compiled_rust_formatter_constructed_fixture_comparison",
            "renderer_version": "5.1.0", "protocols_version": "5.4.0", "dynamo_tokenizers_version": "1.8.0",
            "binary_sha256": sha(binary.read_bytes()), "cargo_lock_sha256": sha((ROOT / "Cargo.lock").read_bytes()),
            "source_sha256": {str(path.relative_to(ROOT)): sha(path.read_bytes())
                              for path in (ROOT / "Cargo.toml", ROOT / "src/main.rs", Path(__file__))},
            "gpu_measurements": False, "model_calls": 0, "constructed_cases": len(rows),
            "text_matches": sum(r.get("text_equal") is True for r in rows),
            "rust_errors": sum(r["rust_status"] == "error" for r in rows),
            "limits": ["The Rust code calls the real pinned standalone formatter; no Dynamo server was launched.",
                       "Python reference uses each official template directly, without Dynamo's message normalization.",
                       "Nemotron reference arguments are mappings; the equivalent OpenAI wire request serializes them as JSON strings.",
                       "Both rendered strings are tokenized by the same pinned Python tokenizer; this does not test Rust tokenization.",
                       "Native formatter selection, model-default merging, multimodal processing, engine prefixes and cache reuse are untested.",
                       "Every message is an authored fixture. No generated model trajectory or performance measurement is implied."],
            "cases": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--qwen-tokenizer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new output path")
    report = compare(args.binary, args.artifacts, args.qwen_tokenizer)
    with args.output.open("x") as file:
        file.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({key: report[key] for key in ("constructed_cases", "text_matches", "rust_errors")}))
