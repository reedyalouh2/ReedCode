"""Check Qwen's prepared prefixes against recorded follow-up requests."""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import importlib.metadata
import json
from pathlib import Path
import struct

MODEL = "Qwen/Qwen3-8B"
REVISION = "b968826d9c46dd6066d109eabc6255188de91218"
TOKENIZER_SHA256 = "aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4"
CONFIG_SHA256 = "d5d09f07b48c3086c508b30d1c9114bd1189145b74e982a265350c923acd8101"
TEMPLATE_SHA256 = "a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8"
ROOT = Path(__file__).resolve().parent
SENTINEL = "REEDCODE_UNKNOWN_FUTURE_CONTENT_29f73695"
END = "<|im_end|>"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def common_prefix(left: list[int], right: list[int]) -> int:
    return next((i for i, pair in enumerate(zip(left, right)) if pair[0] != pair[1]),
                min(len(left), len(right)))


def dynamo_input_sequence_hashes(token_ids: list[int], block_size: int) -> list[int]:
    """Match Dynamo 1.5.0 request-trace hashes, including the final partial block."""
    import xxhash

    if not isinstance(block_size, int) or isinstance(block_size, bool) or block_size <= 0:
        raise ValueError("block_size must be a positive integer")
    if any(not isinstance(token, int) or isinstance(token, bool) or not 0 <= token <= 0xFFFFFFFF
           for token in token_ids):
        raise ValueError("Token IDs must be unsigned 32-bit integers")
    hashes = []
    for start in range(0, len(token_ids), block_size):
        block = token_ids[start:start + block_size]
        local = xxhash.xxh3_64_intdigest(struct.pack(f"<{len(block)}I", *block), seed=1337)
        # Replay hashes use the same parent/local recurrence as the KV router.
        sequence = xxhash.xxh3_64_intdigest(struct.pack("<QQ", hashes[-1], local), seed=1337) if hashes else local
        hashes.append(sequence)
    return hashes


def validate_server_prompt(token_ids: list[int], request_end_record: dict) -> dict:
    """Compare rendered IDs with a saved request_end record; missing evidence stays unknown."""
    result = {"status": "unverifiable", "input_length_matches": None, "hashes_match": None,
              "trace_block_size": None, "compared_hashes": 0, "expected_hashes": None,
              "first_mismatch_block": None, "reason": None}
    if not isinstance(request_end_record, dict):
        result["reason"] = "Missing request_end record"
        return result
    event = request_end_record.get("event", request_end_record)
    if not isinstance(event, dict):
        result["reason"] = "Missing request_end event"
        return result
    if event.get("event_type") not in {None, "request_end"}:
        result["reason"] = "Expected a request_end event"
        return result
    request = event.get("request", event)
    if not isinstance(request, dict) or not isinstance(request.get("replay"), dict):
        result["reason"] = "Missing input hash metadata"
        return result
    replay = request["replay"]
    size, length, expected = (replay.get("trace_block_size"), replay.get("input_length"),
                              replay.get("input_sequence_hashes"))
    if (not isinstance(size, int) or isinstance(size, bool) or size <= 0
            or not isinstance(length, int) or isinstance(length, bool) or length < 0
            or not isinstance(expected, list)
            or any(not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0xFFFFFFFFFFFFFFFF
                   for value in expected)):
        result["reason"] = "Missing or malformed input hash metadata"
        return result
    result.update({"trace_block_size": size, "expected_hashes": len(expected)})
    if len(expected) != (length + size - 1) // size:
        result["reason"] = "Hash count does not cover the recorded input, including its partial block"
        return result
    result["input_length_matches"] = length == len(token_ids)
    if request.get("input_tokens") is not None:
        result["input_length_matches"] &= request["input_tokens"] == len(token_ids)
    try:
        actual = dynamo_input_sequence_hashes(token_ids, size)
    except ModuleNotFoundError:
        result["reason"] = "Install the pinned xxhash dependency to verify saved server hashes"
        return result
    result["compared_hashes"] = min(len(actual), len(expected))
    result["hashes_match"] = actual == expected
    if actual != expected:
        result["first_mismatch_block"] = next(
            (i for i, (left, right) in enumerate(zip(actual, expected)) if left != right),
            min(len(actual), len(expected)))
    result["status"] = "verified" if result["input_length_matches"] and result["hashes_match"] else "mismatch"
    result["reason"] = ("All recorded input hashes match, including the trailing partial block"
                        if result["status"] == "verified" else "Rendered input differs from the server trace")
    return result


def without_nulls(value):
    if isinstance(value, dict):
        return {key: without_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [without_nulls(item) for item in value]
    return value


def check_followup_contract(request: dict, assistant: dict, following: dict) -> None:
    """A context rewrite invalidates a candidate prepared from older history."""
    expected = without_nulls(request["messages"] + [assistant])
    messages = without_nulls(following["messages"])
    if messages[:len(expected)] != expected or len(messages) <= len(expected):
        raise ValueError("Follow-up changed the completed history")
    for key in ("model", "tools", "tool_choice", "chat_template_args", "chat_template_kwargs"):
        if without_nulls(request.get(key)) != without_nulls(following.get(key)):
            raise ValueError(f"Follow-up changed {key}")
    future = messages[len(expected):]
    if assistant.get("tool_calls"):
        expected_ids = [call["id"] for call in assistant["tool_calls"]]
        if (any(message["role"] != "tool" for message in future)
                or [message.get("tool_call_id") for message in future] != expected_ids):
            raise ValueError("Expected the completed assistant's tool results")
    elif len(future) != 1 or future[0]["role"] != "user":
        raise ValueError("Expected one ordinary user continuation")
    elif future[0]["content"].startswith("<tool_response>") and future[0]["content"].endswith("</tool_response>"):
        raise ValueError("Qwen treats this user content as a tool response")


def decode_prefix_bounds(initial: list[int], following: list[int], completion_tokens: int) -> dict:
    """Bound reuse without guessing token IDs lost by response parsing."""
    shared = common_prefix(initial, following)
    if shared < len(initial) or shared == len(following):
        return {"lower_tokens": shared, "upper_tokens": shared,
                "reason": "The mismatch occurs before any sampled output token."}
    return {"lower_tokens": shared,
            "upper_tokens": min(len(following), len(initial) + completion_tokens),
            "reason": "Raw sampled output IDs were not recorded; this is a length bound."}


class QwenRenderer:
    def __init__(self, tokenizer_path: Path):
        from jinja2 import Environment
        from tokenizers import Tokenizer

        raw = tokenizer_path.read_bytes()
        if sha256(raw) != TOKENIZER_SHA256:
            raise ValueError("Tokenizer hash differs from the pinned GPU deployment")
        config_bytes = (ROOT / "fixtures/tokenizer_config.json").read_bytes()
        if sha256(config_bytes) != CONFIG_SHA256:
            raise ValueError("Tokenizer configuration changed")
        self.config = json.loads(config_bytes)
        if sha256(self.config["chat_template"].encode()) != TEMPLATE_SHA256:
            raise ValueError("The candidate construction is specific to the pinned Qwen template")
        environment = Environment(trim_blocks=True, lstrip_blocks=True)
        # Dynamo's renderer uses Python JSON spacing and preserves insertion order.
        environment.filters["tojson"] = lambda value: json.dumps(value, ensure_ascii=False)
        self.template = environment.from_string(self.config["chat_template"])
        self.tokenizer = Tokenizer.from_str(raw.decode())
        if self.tokens(END) != [151645]:
            raise ValueError("Expected Qwen's closed-message special token")

    def tokens(self, text: str) -> list[int]:
        return self.tokenizer.encode(text, add_special_tokens=False).ids

    def render(self, request: dict, messages: list[dict], *, generation: bool,
               include_tools: bool = True) -> str:
        if request.get("model") != MODEL:
            raise ValueError("Only the pinned Qwen3-8B request format is supported")
        if any(message.get("content") is not None and not isinstance(message["content"], str)
               for message in messages):
            raise ValueError("This audit only supports text messages")
        args = request.get("chat_template_args", request.get("chat_template_kwargs", {}))
        if set(args) - {"enable_thinking"}:
            raise ValueError("Unsupported chat-template arguments")
        if request.get("tool_choice") == "none":
            raise ValueError("tool_choice=none has separate renderer filtering; audit it separately")
        return self.template.render(messages=messages,
                                    tools=request.get("tools") if include_tools else None,
                                    add_generation_prompt=generation,
                                    enable_thinking=args.get("enable_thinking", False))

    def candidate(self, request: dict, assistant: dict, *, block_size: int) -> dict:
        """Prepare from completed history only; future contents never enter here."""
        if block_size <= 0:
            raise ValueError("block_size must be positive")
        if assistant.get("role") != "assistant":
            raise ValueError("Expected a completed assistant message")
        if SENTINEL in json.dumps([request, assistant], ensure_ascii=False):
            raise ValueError("Placeholder collision")
        next_role = "tool" if assistant.get("tool_calls") else "user"
        placeholder = {"role": next_role, "content": SENTINEL}
        if next_role == "tool":
            placeholder["tool_call_id"] = assistant["tool_calls"][0]["id"]
        messages = deepcopy(request["messages"]) + [deepcopy(assistant), placeholder]
        rendered = self.render(request, messages, generation=False)
        if rendered.count(SENTINEL) != 1:
            raise ValueError("Future content did not render exactly once")
        before_future = rendered.split(SENTINEL)[0]
        end = before_future.rfind(END)
        if end < 0:
            raise ValueError("No completed assistant boundary")
        # The closed-message token prevents BPE merges with unknown future text.
        text = before_future[:end + len(END)]
        all_ids = self.tokens(text)
        ids = all_ids[:len(all_ids) // block_size * block_size]
        return {"next_role": next_role, "closed_prefix_text": text,
                "closed_prefix_token_ids": all_ids, "prepared_token_ids": ids}


def case_report(renderer: QwenRenderer, name: str, rows: list[dict], block_size: int) -> tuple[dict, dict]:
    first, follow = rows
    choice = first["response"]["choices"][0]
    if choice["finish_reason"] not in {"stop", "tool_calls"}:
        raise ValueError("Incomplete responses cannot produce a candidate")
    request = first["request"]
    assistant = choice["message"]
    initial = renderer.tokens(renderer.render(request, request["messages"], generation=True))
    actual_request = follow["request"]
    check_followup_contract(request, assistant, actual_request)
    actual = renderer.tokens(renderer.render(actual_request, actual_request["messages"], generation=True))
    stock_assistant = {"role": "assistant", "content": assistant.get("content") or ""}
    history = request["messages"]
    candidate = renderer.candidate(request, assistant, block_size=block_size)
    variants = {
        "stock": renderer.tokens(renderer.render(request, history + [stock_assistant],
                                                  generation=False, include_tools=False)),
        "tools_only": renderer.tokens(renderer.render(request, history + [stock_assistant],
                                                       generation=False)),
        "all_fields_without_future_role": renderer.tokens(renderer.render(request, history + [assistant],
                                                                           generation=False)),
        "closed_assistant_candidate": candidate["prepared_token_ids"],
    }
    evidence = {}
    for variant, ids in variants.items():
        prefix = common_prefix(ids, actual)
        evidence[variant] = {"tokens": len(ids), "matching_prefix_tokens": prefix,
                             "matching_full_blocks": prefix // block_size,
                             "entire_candidate_is_prefix": prefix == len(ids)}
    expected = [first["response"]["usage"]["prompt_tokens"],
                follow["response"]["usage"]["prompt_tokens"]]
    if [len(initial), len(actual)] != expected:
        raise ValueError(f"{name}: rendered lengths differ from the GPU capture")
    normal = decode_prefix_bounds(initial, actual, first["response"]["usage"]["completion_tokens"])
    normal["lower_full_blocks"] = normal["lower_tokens"] // block_size
    normal["upper_full_blocks"] = normal["upper_tokens"] // block_size
    corrected_blocks = evidence["closed_assistant_candidate"]["matching_full_blocks"]
    report = {
        "case": name, "initial_prompt_tokens": len(initial), "next_prompt_tokens": len(actual),
        "recorded_prompt_lengths_match": True, "variants": evidence,
        "closed_prefix_tokens_before_block_rounding": len(candidate["closed_prefix_token_ids"]),
        "ordinary_decode_prefix_bounds": normal,
        "candidate_blocks_beyond_ordinary_decode_upper_bound": max(0, corrected_blocks - normal["upper_full_blocks"]),
        "candidate_gpu_cache_reuse": None,
    }
    export = {"case": name, "model": MODEL, "model_revision": REVISION,
              "block_size": block_size, "initial_prompt_token_ids": initial,
              "actual_followup_token_ids": actual,
              "prepared_token_ids": candidate["prepared_token_ids"],
              "closed_prefix_token_ids": candidate["closed_prefix_token_ids"],
              "candidate_uses_future_content": False,
              "template_sha256": TEMPLATE_SHA256, "tokenizer_sha256": TOKENIZER_SHA256,
              "actual_followup_is_for_retrospective_audit_only": True,
              "raw_sampled_output_token_ids": None}
    return report, export


def audit(tokenizer_path: Path, block_size: int = 16) -> tuple[dict, dict]:
    renderer = QwenRenderer(tokenizer_path)
    reports, exports = [], []
    for name in ("text", "tool"):
        path = ROOT / f"fixtures/{name}-on.json"
        report, export = case_report(renderer, name, json.loads(path.read_text()), block_size)
        report["capture_sha256"] = sha256(path.read_bytes())
        reports.append(report)
        exports.append(export)
    result = {
        "schema_version": 1, "evidence": "local_rendering_audit",
        "model": MODEL, "model_revision": REVISION, "block_size": block_size,
        "tokenizer_sha256": TOKENIZER_SHA256, "template_sha256": TEMPLATE_SHA256,
        "renderer": {"engine": "Jinja2", "version": importlib.metadata.version("jinja2"),
                     "tokenizers_version": importlib.metadata.version("tokenizers"),
                     "rust_renderer_executed": False},
        "cases": reports,
        "gpu_validation": "The corrected candidate has not run on a GPU.",
    }
    return result, {"schema_version": 1, "evidence": "local_rendering_audit", "cases": exports}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--block-size", type=int, default=16)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--export", type=Path, help="Write token IDs for a separate GPU probe")
    args = parser.parse_args()
    result, export = audit(args.tokenizer, args.block_size)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    else:
        print(encoded, end="")
    if args.export:
        args.export.write_text(json.dumps(export, indent=2) + "\n")


if __name__ == "__main__":
    main()
