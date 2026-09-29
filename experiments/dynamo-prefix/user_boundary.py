"""Isolate new-user template rewrites with explicitly constructed Qwen sessions."""

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import audit

ROOT = Path(__file__).resolve().parent


def fixture(reasoning_words, tool_lines):
    if type(reasoning_words) is not int or reasoning_words < 0 or type(tool_lines) is not int or tool_lines < 1:
        raise ValueError("Expected nonnegative reasoning_words and positive tool_lines")
    tools = [{"type": "function", "function": {"name": "read_file", "description": "Read a file.",
               "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]
    history = [{"role": "system", "content": "Fix pricing.py and check its tests."}]
    instructions = ["Fix the percentage calculation.", "Add tests for fractional discounts.",
                    "Reject discounts outside the supported range.", "Add a regression test for zero prices."]
    pairs = []
    for index, instruction in enumerate(instructions):
        history.append({"role": "user", "content": instruction})
        assistant = {"role": "assistant", "content": "I will inspect the file.", "tool_calls": [
            {"id": f"read-{index}", "type": "function", "function": {
                "name": "read_file", "arguments": '{"path": "pricing.py"}'}}]}
        if reasoning_words:
            assistant["reasoning_content"] = "synthetic " * reasoning_words
        history.extend([assistant, {"role": "tool", "tool_call_id": f"read-{index}",
                        "content": "".join(f"diagnostic line {line}: price and discount fixture\n" for line in range(tool_lines))}])
        request = {"model": audit.MODEL, "tools": tools, "messages": deepcopy(history),
                   "chat_template_kwargs": {"enable_thinking": True}}
        completed = {"role": "assistant", "content": "Fixture response. No code was executed."}
        history.append(completed)
        if index < len(instructions) - 1:
            following = deepcopy(request)
            following["messages"] = deepcopy(history) + [{"role": "user", "content": instructions[index + 1]}]
            pairs.append({"user_boundary": index + 1, "request": request, "completed_assistant": completed,
                          "following": following})
    return pairs


def measure(renderer, pair):
    request, following = pair["request"], pair["following"]
    audit.check_followup_contract(request, pair["completed_assistant"], following)
    old_text = renderer.render(request, request["messages"], generation=True)
    new_text = renderer.render(following, following["messages"], generation=True)
    old_ids, new_ids = renderer.tokens(old_text), renderer.tokens(new_text)
    shared = audit.common_prefix(old_ids, new_ids)
    stable = shared // 16 * 16
    body = renderer.tokens(renderer.render(request, request["messages"], generation=False))
    # Only appended messages differ; an earlier divergence belongs to template rendering.
    rewrote_history = shared < len(body)
    first_old = old_ids[shared:shared + 8]
    first_new = new_ids[shared:shared + 8]
    cause = ("template_history_rewrite" if rewrote_history else
             "append_only" if shared == len(old_ids) else "generation_boundary")
    return {"user_boundary": pair["user_boundary"], "cause": cause,
            "prior_input_tokens": len(old_ids), "following_input_tokens": len(new_ids),
            "common_prefix_tokens": shared, "common_complete_block_tokens": stable,
            "prior_complete_block_tokens_past_divergence": len(old_ids) // 16 * 16 - stable,
            "following_tokens_past_shared_blocks": len(new_ids) - stable,
            "old_reasoning_marker_present": "<think>\nsynthetic " in old_text,
            "following_reasoning_marker_present": "<think>\nsynthetic " in new_text,
            "first_differing_old_ids": first_old, "first_differing_new_ids": first_new,
            "old_window": renderer.tokenizer.decode(first_old, skip_special_tokens=False),
            "new_window": renderer.tokenizer.decode(first_new, skip_special_tokens=False)}


def run(tokenizer):
    renderer = audit.QwenRenderer(tokenizer)
    cases = []
    for words, lines in ((0, 16), (1, 16), (256, 16), (2048, 16), (1, 512), (2048, 512)):
        pairs = fixture(words, lines)
        cases.append({"synthetic_reasoning_words_per_user_turn": words, "synthetic_tool_lines_per_user_turn": lines,
                      "fixture_sha256": hashlib.sha256(json.dumps(pairs, ensure_ascii=False).encode()).hexdigest(),
                      "boundaries": [measure(renderer, pair) for pair in pairs]})
    return {"schema_version": 1, "evidence": "constructed_template_mechanism_probe",
            "real_agent_sessions": 0, "model_calls": 0, "gpu_measurements": False,
            "model": audit.MODEL, "model_revision": audit.REVISION,
            "tokenizer_sha256": audit.TOKENIZER_SHA256, "template_sha256": audit.TEMPLATE_SHA256,
            "source_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in (Path(__file__), ROOT / "audit.py")},
            "limits": ["All reasoning, tool output, and assistant replies are authored fixtures.",
                       "The four user instructions were never executed or graded.",
                       "Inputs use the pinned template; no thinking-on server fingerprints were collected.",
                       "Lost prior blocks include removed reasoning; they do not measure work the next request must repeat.",
                       "Next-input suffix counts include new material; neither token count predicts GPU time.",
                       "ReedCode has no compaction path; this probe introduces none.",
                       "Frequency, shared-cache residency, quality, and concurrency effects remain unmeasured."],
            "cases": cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new output path")
    result = run(args.tokenizer)
    with args.output.open("x") as file:
        file.write(json.dumps(result, indent=2) + "\n")
    for case in result["cases"]:
        first = case["boundaries"][0]
        print(f"reasoning words={case['synthetic_reasoning_words_per_user_turn']}, "
              f"tool lines={case['synthetic_tool_lines_per_user_turn']}: "
              f"{first['cause']}, old blocks beyond divergence="
              f"{first['prior_complete_block_tokens_past_divergence']}, "
              f"next suffix={first['following_tokens_past_shared_blocks']}")


if __name__ == "__main__":
    main()
