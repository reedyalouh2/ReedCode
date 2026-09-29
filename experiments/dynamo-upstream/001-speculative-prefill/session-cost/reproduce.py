"""Measure incompatible stock warmup inputs and known-prefix coverage on CPU."""

import argparse
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
from statistics import mean, median
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
CURRENT = ROOT.parent / "current-code"
PREFIX = ROOT.parents[2] / "dynamo-prefix"
sys.path.insert(0, str(PREFIX))
import audit
import survey


def sha(data):
    return hashlib.sha256(data).hexdigest()


def prefix_coverage(tokens, previous, block_size=16):
    if type(block_size) is not int or block_size <= 0:
        raise ValueError("block_size must be a positive integer")
    # vLLM leaves an input token to compute the next logits, even on a full hit.
    reusable_limit = max(0, len(tokens) - 1)
    return max((min(audit.common_prefix(tokens, other), reusable_limit) // block_size * block_size
                for other in previous), default=0)


def cost(prepared, following, normal_inputs, prior_warmups, block_size=16):
    shared = audit.common_prefix(prepared, following)
    full_tokens = len(prepared) // block_size * block_size
    compatible = shared // block_size * block_size
    normal = prefix_coverage(prepared, normal_inputs, block_size)
    with_warmups = prefix_coverage(prepared, normal_inputs + prior_warmups, block_size)
    return {
        "prepared_tokens": len(prepared), "followup_tokens": len(following),
        "first_different_token": shared, "prepared_is_prefix": shared == len(prepared),
        "incompatible_suffix_tokens": len(prepared) - shared,
        "nonmatching_full_blocks": (full_tokens - compatible) // block_size,
        "nonmatching_full_block_tokens": full_tokens - compatible,
        "trailing_partial_tokens": len(prepared) % block_size,
        "known_normal_input_prefix_tokens": normal,
        "known_normal_and_warmup_prefix_tokens": with_warmups,
        "uncovered_input_tokens_normal_only": len(prepared) - normal,
        "uncovered_input_tokens_with_prior_warmups": len(prepared) - with_warmups,
        "uncovered_full_block_tokens_with_prior_warmups": max(0, full_tokens - with_warmups),
        "prior_warmups_add_coverage": with_warmups > normal,
    }


def recorded_cases(records):
    manifest = json.loads(records["pilot/manifest.json"])
    cases, metadata, sessions = [], [], []
    for run in manifest["runs"]:
        trace = records["pilot/" + run["trace"]]
        if sha(trace) != run["trace_sha256"]:
            raise ValueError("Pilot trace differs from its manifest")
        events = [json.loads(line) for line in trace.splitlines() if line]
        requests, assistants, inference = (survey.by_turn(events, kind) for kind in
                                           ("request", "assistant_message", "inference"))
        turns = sorted(requests)
        if turns != list(range(1, len(turns) + 1)) or set(turns) != set(assistants) or set(turns) != set(inference):
            raise ValueError("Incomplete session capture")
        sessions.append({"run_id": run["run_id"], "source_condition": run["condition"],
                         "model_calls": len(turns), "tool_continuations": len(turns) - 1,
                         "terminal_warmups_excluded": 1, "trace_sha256": sha(trace)})
        for before, after in zip(turns, turns[1:]):
            request, following = requests[before]["request"], requests[after]["request"]
            assistant = assistants[before]["message"]
            audit.check_followup_contract(request, assistant, following)
            if not assistant.get("tool_calls") or inference[before]["finish_reason"] != "tool_calls":
                raise ValueError("Expected a completed tool continuation")
            name = f"{run['run_id']}/{before}"
            cases.append({"name": name, "original_request": request, "followup_request": following,
                          "response_text": assistant.get("content") or "",
                          "effective_normal_template_args": {"enable_thinking": False}})
            metadata.append({"case": name, "run_id": run["run_id"], "source_condition": run["condition"],
                             "before_turn": before, "after_turn": after,
                             "original_id": inference[before]["completion_id"].removeprefix("chatcmpl-"),
                             "followup_id": inference[after]["completion_id"].removeprefix("chatcmpl-"),
                             "tool_calls": len(assistant["tool_calls"]),
                             "source_hint_enabled": run["condition"] == "on"})
    return cases, metadata, sessions


def growth_cases(cases, renderer):
    run = cases[0]["name"].rsplit("/", 1)[0]
    pair = [next(c for c in cases if c["name"] == f"{run}/{turn}") for turn in (2, 3)]
    history_slot = next(i for i, m in enumerate(pair[0]["original_request"]["messages"]) if m["role"] == "tool")
    system_slot = next(i for i, m in enumerate(pair[0]["original_request"]["messages"]) if m["role"] == "system")
    output, metadata = [], []
    for location, targets in (("earlier_tool_output", (4096, 8192, 16384, 32768, 50000)),
                              ("system_content", (50000,))):
        slot = history_slot if location == "earlier_tool_output" else system_slot
        for target in targets:
            template = deepcopy(pair[0]["original_request"])
            original_text = template["messages"][slot]["content"]
            count = max(0, target - len(renderer.tokens(renderer.render(template, template["messages"], generation=True))))
            for _ in range(4):
                template["messages"][slot]["content"] = original_text + " trace" * count
                measured = len(renderer.tokens(renderer.render(template, template["messages"], generation=True)))
                if measured == target:
                    break
                count = max(0, count + target - measured)
            if measured != target:
                raise ValueError("Could not construct the requested length control")
            for step, base in enumerate(pair, 1):
                copied = deepcopy(base)
                name = f"constructed/{location}/{target}/{step}"
                copied["name"] = name
                for key in ("original_request", "followup_request"):
                    copied[key]["messages"][slot]["content"] = template["messages"][slot]["content"]
                output.append(copied)
                metadata.append({"case": name, "group": f"{location}/{target}", "step": step,
                                 "target_first_input_tokens": target, "placement": location,
                                 "filler": "repeated literal ' trace' appended to captured content",
                                 "source_pair": base["name"], "real_generated_session": False})
    return output, metadata


def summarize(rows):
    keys = ("prepared_tokens", "incompatible_suffix_tokens", "nonmatching_full_block_tokens",
            "uncovered_input_tokens_normal_only", "uncovered_input_tokens_with_prior_warmups",
            "uncovered_full_block_tokens_with_prior_warmups")
    return {"transitions": len(rows), "fields": {
        k: {"sum": sum(r[k] for r in rows), "mean": mean(r[k] for r in rows),
            "median": median(r[k] for r in rows), "min": min(r[k] for r in rows), "max": max(r[k] for r in rows)}
        for k in keys}, "first_different_tokens": sorted({r["first_different_token"] for r in rows}),
        "prior_warmups_add_coverage": sum(r["prior_warmups_add_coverage"] for r in rows)}


def run(args):
    build = json.loads(args.build_report.read_text())
    native = next(r for r in build["runs"] if r["label"] == args.source)
    if sha(args.binary.read_bytes()) != native["binary_sha256"]:
        raise ValueError("Binary differs from its build report; pass the report from that build")
    if build["source_manifest_sha256"] != sha((CURRENT / "sources/manifest.json").read_bytes()):
        raise ValueError("Build uses different upstream source pins")
    if native["registry_dependency_audit"]["differences_from_upstream_lock"]:
        raise ValueError("Build dependency graph differs from upstream")
    renderer = audit.QwenRenderer(args.tokenizer)
    records = survey.read_records(survey.ARCHIVE)
    cases, metadata, sessions = recorded_cases(records)
    constructed, construction_metadata = growth_cases(cases, renderer)
    input_data = {"config_path": str((PREFIX / "fixtures/tokenizer_config.json").resolve()),
                  "tokenizer_path": str(args.tokenizer.resolve()), "cases": cases + constructed}
    input_bytes = json.dumps(input_data, indent=2).encode()
    args.output.mkdir(parents=True)
    input_path = args.output / "input.json"
    input_path.write_bytes(input_bytes)
    raw = subprocess.check_output([str(args.binary.resolve()), str(input_path.resolve())])
    output = json.loads(raw)
    if [r["case"] for r in output] != [c["name"] for c in cases + constructed]:
        raise ValueError("Native runner did not return all requested cases in order")
    for row in output:
        traits = row["stock_request_traits"]
        if traits["tools"] is not None or traits["chat_template_args"] is not None or traits["add_generation_prompt"]:
            raise ValueError("Runner does not use the stock request defaults")
    server = survey.request_end_index(records["server/frontend-trace.jsonl"])
    known, verified, rows = {}, {}, []
    for info, rendered in zip(metadata, output[:len(cases)]):
        group = known.setdefault(info["run_id"], {"normal": [], "warm": []})
        for side, request_id in (("original", info["original_id"]), ("followup", info["followup_id"])):
            ids = rendered[side]["token_ids"]
            checked = audit.validate_server_prompt(ids, server[request_id])
            if checked["status"] != "verified":
                raise ValueError(f"Native normal prompt differs from the saved server: {info['case']}/{side}")
            verified[request_id] = checked["compared_hashes"]
        group["normal"].append(rendered["original"]["token_ids"])
        measured = cost(rendered["prepared"]["token_ids"], rendered["followup"]["token_ids"],
                        group["normal"], group["warm"])
        rows.append({**info, "original_input_tokens": len(rendered["original"]["token_ids"]), **measured})
        group["warm"].append(rendered["prepared"]["token_ids"])
    growth, known = [], {}
    for info, rendered in zip(construction_metadata, output[len(cases):]):
        group = known.setdefault(info["group"], {"normal": [], "warm": []})
        group["normal"].append(rendered["original"]["token_ids"])
        growth.append({**info, "original_input_tokens": len(rendered["original"]["token_ids"]),
                       **cost(rendered["prepared"]["token_ids"], rendered["followup"]["token_ids"],
                              group["normal"], group["warm"])})
        group["warm"].append(rendered["prepared"]["token_ids"])
    evidence = {}
    for name, data in (("input.json.gz", input_bytes), ("native-output.json.gz", raw)):
        compressed = gzip.compress(data, mtime=0)
        (args.output / name).write_bytes(compressed)
        evidence[name] = {"sha256": sha(compressed), "uncompressed_sha256": sha(data),
                          "uncompressed_bytes": len(data)}
    input_path.unlink()
    result = {
        "evidence": "native_cpu_reconstruction_and_input_prefix_counterfactual",
        "source_revision": native["revision"], "source": args.source,
        "binary_sha256": native["binary_sha256"], "build_report_sha256": sha(args.build_report.read_bytes()),
        "script_sha256": sha(Path(__file__).read_bytes()), "archive_sha256": sha(survey.ARCHIVE.read_bytes()),
        "tokenizer_sha256": audit.TOKENIZER_SHA256, "template_sha256": audit.TEMPLATE_SHA256,
        "raw_evidence": evidence, "new_model_requests": 0, "gpu_measurements": False,
        "verified_unique_normal_requests": len(verified), "verified_normal_hashes": sum(verified.values()),
        "sessions": sessions, "summary": summarize(rows),
        "by_source_condition": {condition: summarize([r for r in rows if r["source_condition"] == condition])
                                for condition in ("on", "off")},
        "context_bands": [{"original_input_range": [low, high], **summarize(chosen)}
                          for low, high in ((0, 1024), (1024, 2048), (2048, 3072), (3072, 6144))
                          if (chosen := [r for r in rows if low <= r["original_input_tokens"] < high])],
        "transitions": rows, "constructed_growth": growth,
        "assumptions": [
            "Input-only counterfactual starts each session cold and retains all prior completed input blocks without eviction.",
            "Every hypothetical earlier warmup completes and its blocks remain available before the next warmup.",
            "Shared model/cache identity, 16-token blocks; no cross-session cache entries or sampled decode token IDs are modeled.",
            "Prefix-hit coverage leaves at least one input token for the vLLM next-logit computation.",
            "Warmups from source-off sessions are hypothetical. No current GPU cache-miss or block-insertion count was collected.",
        ],
        "limits": [
            "Incompatible suffix tokens and unmatched full blocks are not measured GPU prefill work.",
            "Incompatibility is relative to the observed next real request; later warmups can reuse those tokens.",
            "The ten terminal assistant responses have no observed continuation and are excluded from this estimate.",
            "Length controls append repetitive filler to captured content; they are not realistic generated 50K sessions.",
            "The original deployment max-model-len was 32768; longer constructed inputs ran only through CPU rendering.",
            "No latency, cache eviction, inserted-block, cross-agent or multi-worker impact is established.",
        ],
    }
    (args.output / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--build-report", type=Path, default=CURRENT / "results.json")
    parser.add_argument("--source", choices=("release", "main"), default="main")
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new output directory")
    run(args)
