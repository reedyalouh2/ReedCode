"""Verify the archived stock-hint requests and reproduce their prefix mismatch on CPU."""

import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
import tarfile

ROOT = Path(__file__).resolve().parent
PREFIX = ROOT.parents[1] / "dynamo-prefix"
sys.path.insert(0, str(PREFIX))
import audit
import survey


def local_hashes(ids, block_size):
    import xxhash
    if type(block_size) is not int or block_size <= 0:
        raise ValueError("block_size must be a positive integer")
    return [xxhash.xxh3_64_intdigest(struct.pack(f"<{block_size}I", *ids[i:i + block_size]), seed=1337)
            for i in range(0, len(ids) - block_size + 1, block_size)]


def check_routing(ids, recorded):
    size = recorded["block_size"]
    hashes = json.loads(recorded["local_hashes"])
    if (len(ids) != recorded["isl_tokens"] or local_hashes(ids, size) != hashes
            or len(hashes) != recorded["num_blocks"]):
        raise ValueError("Local reconstruction differs from the recorded routing input")
    return {"verified_full_blocks": len(hashes), "unhashed_trailing_tokens": len(ids) % size}


def window(renderer, ids, start):
    chosen = ids[max(0, start - 4):start + 12]
    return {"start_token_index": max(0, start - 4), "token_ids": chosen,
            "text": renderer.tokenizer.decode(chosen, skip_special_tokens=False)}


def verify_bundle(records):
    manifest = json.loads((ROOT / "provenance-manifest.json").read_text())
    bundle = ROOT / "provenance-evidence.tar.gz"
    with tarfile.open(bundle) as archive:
        for name, info in manifest["files"].items():
            source = records[info["source_member"]]
            if hashlib.sha256(source).hexdigest() != info["source_member_sha256"]:
                raise ValueError(f"Bundle source changed: {name}")
            if "source_line_numbers_1based" in info:
                lines = source.splitlines(keepends=True)
                source = b"".join(lines[n - 1] for n in info["source_line_numbers_1based"])
            copied = archive.extractfile(name).read()
            if source != copied or hashlib.sha256(copied).hexdigest() != info["sha256"]:
                raise ValueError(f"Bundle extraction differs: {name}")
    return {"sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(), "verified_files": len(manifest["files"])}


def reproduce(tokenizer, archive=survey.ARCHIVE):
    records = survey.read_records(archive)
    bundle = verify_bundle(records)
    renderer = audit.QwenRenderer(tokenizer)
    deployment = json.loads(records["setup/deployment.json"])
    if deployment["model_revision"] != audit.REVISION or deployment["thinking_mode"] != "disabled":
        raise ValueError("Unexpected archived model deployment")
    log = [json.loads(line) for line in records["server/frontend.log"].decode().splitlines() if line.startswith("{")]
    routing = [r for r in log if r.get("message") == "[ROUTING_INPUT] request local hashes"]
    by_id = {r["request_id"]: (i, r) for i, r in enumerate(routing) if r.get("request_id")}
    events = [json.loads(line)["event"] for line in records["server/frontend-trace.jsonl"].decode().splitlines()]
    payloads = {e["payload"]["request_id"]: e["payload"] for e in events if e["event_type"] == "request_payload"}
    ends = {e["request"]["request_id"]: e for e in events if e["event_type"] == "request_end"}
    cases = []
    for name in ("text", "tool"):
        rows = json.loads(records[f"prefix-check/{name}-on.json"])
        ids = [r["response"]["id"].removeprefix("chatcmpl-") for r in rows]
        first_at, first_log = by_id[ids[0]]
        next_at, next_log = by_id[ids[1]]
        candidates = routing[first_at + 1:next_at]
        if len(candidates) != 1 or candidates[0].get("request_id"):
            raise ValueError("Expected exactly one isolated internal preparation")
        prepared_log = candidates[0]
        request, following = rows[0]["request"], rows[1]["request"]
        assistant = rows[0]["response"]["choices"][0]["message"]
        audit.check_followup_contract(request, assistant, following)
        rendered = []
        for captured, request_id, route in zip(rows, ids, (first_log, next_log)):
            wire = payloads[request_id]
            if (wire["endpoint"] != "openai.chat_completion"
                    or wire["request"]["nvext"]["agent_hints"]["speculative_prefill"] is not True
                    or not wire["payload_complete"]):
                raise ValueError("Missing complete server evidence of the stock Chat hint")
            body = captured["request"]
            for key in ("messages", "tools", "tool_choice", "model"):
                if wire["request"].get(key) != body.get(key):
                    raise ValueError("Client capture differs from the server request")
            tokens = renderer.tokens(renderer.render(body, body["messages"], generation=True))
            if audit.validate_server_prompt(tokens, ends[request_id])["status"] != "verified":
                raise ValueError("Client prompt differs from server input fingerprints")
            check_routing(tokens, route)
            rendered.append(tokens)
        # This reproduces the deployed text-only builder; it is not a corrected adapter.
        stock_assistant = {"role": "assistant", "content": assistant.get("content") or ""}
        prepared = renderer.tokens(renderer.render(request, request["messages"] + [stock_assistant],
                                                    generation=False, include_tools=False))
        warmed_evidence = check_routing(prepared, prepared_log)
        first, actual = rendered
        divergence = audit.common_prefix(prepared, actual)
        shared_original = audit.common_prefix(first, actual) // 16 * 16
        shared_prepared = divergence // 16 * 16
        off = json.loads(records[f"prefix-check/{name}-off.json"])
        if any(capture[0]["response"]["usage"]["prompt_tokens_details"]["cached_tokens"] != 0
               for capture in (off, rows)):
            raise ValueError("Initial request was not cold in a captured condition")
        cases.append({"case": name, "source": "stock_nvext_agent_hints_speculative_prefill",
                      "original_request_id": ids[0], "followup_request_id": ids[1],
                      "speculative_tokens": len(prepared), "followup_tokens": len(actual),
                      "prepared_input_matches_routing_hashes": warmed_evidence,
                      "exact_divergence_from_cpu_reconstruction": divergence,
                      "runtime_first_differing_block": divergence // 16,
                      "leading_prepared_tokens_in_matching_full_blocks": shared_prepared,
                      "leading_original_tokens_in_matching_full_blocks": shared_original,
                      "additional_matching_full_block_tokens": max(0, shared_prepared - shared_original),
                      "cached_followup_off": off[1]["response"]["usage"]["prompt_tokens_details"]["cached_tokens"],
                      "cached_followup_on": rows[1]["response"]["usage"]["prompt_tokens_details"]["cached_tokens"],
                      "prepared_window": window(renderer, prepared, divergence),
                      "followup_window": window(renderer, actual, divergence)})
    return {"schema_version": 1, "verdict": "weakness_confirmed_on_recorded_1.5.0_deployment",
            "evidence": "archived_stock_gpu_execution_plus_cpu_reconstruction",
            "new_gpu_requests": 0, "deployment": deployment,
            "archive_sha256": hashlib.sha256(Path(archive).read_bytes()).hexdigest(),
            "extracted_evidence": bundle,
            "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in (Path(__file__), PREFIX / "audit.py", PREFIX / "survey.py")},
            "tokenizer_sha256": audit.TOKENIZER_SHA256, "template_sha256": audit.TEMPLATE_SHA256,
            "cases": cases,
            "limits": ["Exact speculative token IDs are reconstructed; runtime logs contain lengths and full-block local hashes.",
                       "The tool preparation has eight trailing tokens outside the recorded block hashes.",
                       "The mismatch proves a non-prefix preparation. It does not establish which blocks persisted in cache.",
                       "Zero gain means no additional full-block reuse in these two checks, not zero shared tokens.",
                       "No current-main GPU validation, latency improvement, or multi-worker claim."]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--archive", type=Path, default=survey.ARCHIVE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = reproduce(args.tokenizer, args.archive)
    if args.output:
        with args.output.open("x") as file:
            file.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
