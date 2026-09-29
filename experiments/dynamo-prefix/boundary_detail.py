"""Locate the exact prompt boundary change in the archived coding pilot."""

import argparse
from collections import Counter
import importlib.metadata
import json
from pathlib import Path

import audit
import survey


ROOT = Path(__file__).resolve().parent
EMPTY_THINKING_SUFFIX = [151667, 271, 151668, 271]


def token_window(renderer, ids, start, end):
    selected = ids[start:end]
    return {"start_token_index": start, "token_ids": selected,
            "text": renderer.tokenizer.decode(selected, skip_special_tokens=False)}


def compare_boundary(renderer, initial_ids, following_ids, block_size=16):
    if type(block_size) is not int or block_size <= 0:
        raise ValueError("block_size must be a positive integer")
    common = audit.common_prefix(initial_ids, following_ids)
    tail = initial_ids[common:]
    classification = ("empty_thinking_suffix_removed" if tail == EMPTY_THINKING_SUFFIX
                      else "initial_prompt_is_prefix" if common == len(initial_ids) else "other")
    return {
        "initial_prompt_tokens": len(initial_ids), "following_prompt_tokens": len(following_ids),
        "first_mismatch_token_index": common if common < min(len(initial_ids), len(following_ids)) else None,
        "common_prefix_tokens": common,
        "tokens_remaining_in_initial_input": len(tail),
        "initial_suffix_token_ids": tail,
        "initial_suffix_text": renderer.tokenizer.decode(tail, skip_special_tokens=False),
        "following_first_differing_token_id": following_ids[common] if common < len(following_ids) else None,
        "following_starts_with_tool_call": common < len(following_ids) and following_ids[common] == 151657,
        "initial_input_length_mod_block_size": len(initial_ids) % block_size,
        "mismatch_offset_in_block": common % block_size,
        "previous_complete_block_tokens_lost": (len(initial_ids) // block_size - common // block_size) * block_size,
        "classification": classification,
        "initial_window": token_window(renderer, initial_ids, max(0, common - 3), len(initial_ids)),
        "following_window": token_window(renderer, following_ids, max(0, common - 3), common + 12),
    }


def boundary_detail(tokenizer, archive_path=survey.ARCHIVE):
    renderer = audit.QwenRenderer(Path(tokenizer))
    records = survey.read_records(archive_path)
    manifest = json.loads(records["pilot/manifest.json"])
    deployment = manifest["deployment"]
    if (manifest["model"] != audit.MODEL or deployment["model_revision"] != audit.REVISION
            or deployment.get("thinking_mode") != "disabled"):
        raise ValueError("Expected the pinned thinking-disabled pilot")
    server = survey.request_end_index(records["server/frontend-trace.jsonl"])
    transitions, requests = [], []
    for run in manifest["runs"]:
        content = records["pilot/" + run["trace"]]
        if audit.sha256(content) != run["trace_sha256"]:
            raise ValueError("Trace differs from the pilot manifest")
        events = [json.loads(line) for line in content.decode().splitlines() if line.strip()]
        captured = survey.by_turn(events, "request")
        inference = survey.by_turn(events, "inference")
        assistants = survey.by_turn(events, "assistant_message")
        token_ids = {}
        for turn, row in sorted(captured.items()):
            request = row["request"]
            ids = renderer.tokens(renderer.render(request, request["messages"], generation=True))
            request_id = inference[turn]["completion_id"].removeprefix("chatcmpl-")
            checked = audit.validate_server_prompt(ids, server[request_id])
            if checked["status"] != "verified" or checked["trace_block_size"] != 16:
                raise ValueError(f"{run['run_id']} turn {turn}: input fingerprints do not match")
            token_ids[turn] = ids
            requests.append({"run_id": run["run_id"], "turn": turn, "request_id": request_id,
                             "input_tokens": len(ids), "verified_input_hashes": checked["compared_hashes"]})
        for before, after in zip(sorted(captured), sorted(captured)[1:]):
            if after != before + 1:
                raise ValueError("Missing adjacent request")
            message = assistants[before]["message"]
            audit.check_followup_contract(captured[before]["request"], message, captured[after]["request"])
            row = compare_boundary(renderer, token_ids[before], token_ids[after])
            row.update(run_id=run["run_id"], source_condition=run["condition"], from_turn=before, to_turn=after,
                       assistant_has_content=bool(message.get("content")),
                       assistant_has_nonempty_reasoning=bool(message.get("reasoning_content")))
            transitions.append(row)
    return {
        "schema_version": 1, "evidence": "retrospective_prompt_boundary_detail",
        "new_model_calls": 0, "raw_sampled_output_ids_available": False,
        "token_indices": "zero_based",
        "model": audit.MODEL, "model_revision": audit.REVISION,
        "tokenizer_sha256": audit.TOKENIZER_SHA256, "template_sha256": audit.TEMPLATE_SHA256,
        "source_archive_sha256": audit.sha256(Path(archive_path).read_bytes()),
        "source_sha256": {path.name: audit.sha256(path.read_bytes())
                          for path in (Path(__file__), ROOT / "audit.py", ROOT / "survey.py")},
        "runtime_versions": {name: importlib.metadata.version(name) for name in ("jinja2", "tokenizers", "xxhash")},
        "coverage": {"runs": len(manifest["runs"]), "verified_requests": len(requests),
                     "verified_input_hashes": sum(row["verified_input_hashes"] for row in requests),
                     "continuations": len(transitions)},
        "summary": {
            "classifications": dict(sorted(Counter(row["classification"] for row in transitions).items())),
            "initial_tokens_remaining_at_mismatch": dict(sorted(Counter(row["tokens_remaining_in_initial_input"] for row in transitions).items())),
            "initial_input_length_mod_16": dict(sorted(Counter(row["initial_input_length_mod_block_size"] for row in transitions).items())),
            "lost_previous_complete_block_tokens": dict(sorted(Counter(row["previous_complete_block_tokens_lost"] for row in transitions).items())),
            "following_starts_with_tool_call": sum(row["following_starts_with_tool_call"] for row in transitions),
            "following_starts_with_assistant_prose": sum(row["assistant_has_content"] for row in transitions),
            "assistants_with_nonempty_reasoning": sum(row["assistant_has_nonempty_reasoning"] for row in transitions),
        },
        "empty_thinking_suffix": {
            "token_ids": EMPTY_THINKING_SUFFIX,
            "text": renderer.tokenizer.decode(EMPTY_THINKING_SUFFIX, skip_special_tokens=False)},
        "limits": ["These are locally reconstructed input boundaries verified against saved server hashes.",
                   "Raw sampled output IDs are absent. The four-token mismatch is already inside the input.",
                   "Complete-block loss describes token compatibility; it does not measure cache residency or GPU work.",
                   "This pilot has no new ordinary-user turns or nonempty assistant reasoning to test older-history rewrites."],
        "requests": requests, "transitions": transitions,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--archive", type=Path, default=survey.ARCHIVE)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new output path; existing records are preserved")
    result = boundary_detail(args.tokenizer, args.archive)
    with args.output.open("x") as output:
        output.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in ("coverage", "summary", "empty_thinking_suffix")}, indent=2))


if __name__ == "__main__":
    main()
