"""Count the extra full input blocks retained by stock warmups on CPU."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path
from statistics import mean, median


ROOT = Path(__file__).resolve().parent
SESSION_COST = ROOT.parent / "session-cost"
REPORT_SHA256 = "2181c832eb662abe44a0a8f32464c7ffe75ccb51ffa7b204d3126234a492fe3f"
CONFIG_SHA256 = "f7c4eadfbbf522470667b797a3c89be2524832d2d599797248dc304fff447c30"
MODEL_REVISION = "b968826d9c46dd6066d109eabc6255188de91218"
CONFIG_URL = f"https://huggingface.co/Qwen/Qwen3-8B/resolve/{MODEL_REVISION}/config.json"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def kv_bytes_per_token(config, element_bytes=2):
    if config.get("use_sliding_window"):
        raise ValueError("This accounting requires full attention in every layer")
    if type(element_bytes) is not int or element_bytes <= 0:
        raise ValueError("element_bytes must be a positive integer")
    return 2 * config["num_hidden_layers"] * config["num_key_value_heads"] * config["head_dim"] * element_bytes


class BlockIdentity:
    def __init__(self, block_size=16):
        if type(block_size) is not int or block_size <= 0:
            raise ValueError("block_size must be a positive integer")
        self.block_size = block_size
        self.nodes = {}

    def blocks(self, tokens):
        parent = 0
        result = set()
        for start in range(0, len(tokens) - self.block_size + 1, self.block_size):
            # Parent identity prevents identical text on different branches from merging.
            key = (parent, tuple(tokens[start:start + self.block_size]))
            parent = self.nodes.setdefault(key, len(self.nodes) + 1)
            result.add(parent)
        return result


def snapshot(real, warm, current, current_tokens, bytes_per_block, block_size=16):
    if not current <= real:
        raise ValueError("Current input must already belong to real input history")
    warm_only = warm - real
    all_blocks = real | warm
    denominator = len(current)
    counts = {
        "current_real_blocks": denominator,
        "real_history_blocks": len(real),
        "older_real_blocks_outside_current_context": len(real - current),
        "warmup_history_blocks": len(warm),
        "warmup_only_blocks": len(warm_only),
        "warmup_blocks_also_in_real_history": len(warm & real),
        "combined_blocks": len(all_blocks),
    }
    return {
        "current_real_tokens": current_tokens,
        "current_real_partial_tokens_excluded": current_tokens % block_size,
        **counts,
        "kv_bytes": {key.removesuffix("_blocks"): value * bytes_per_block for key, value in counts.items()},
        "warmup_only_to_current_real_ratio": len(warm_only) / denominator if denominator else None,
        "combined_to_current_real_ratio": len(all_blocks) / denominator if denominator else None,
        "warmup_only_to_real_history_ratio": len(warm_only) / len(real) if real else None,
    }


def session(rows, native, bytes_per_token, block_size=16):
    identities = BlockIdentity(block_size)
    real, warm, steps = set(), set(), []
    for position, row in enumerate(rows, 1):
        rendered = native[row["case"]]
        original = rendered["original"]["token_ids"]
        prepared = rendered["prepared"]["token_ids"]
        following = rendered["followup"]["token_ids"]
        current = identities.blocks(original)
        real.update(current)
        warm.update(identities.blocks(prepared))
        steps.append({"case": row["case"], "step": position, "stage": "after_warmup",
                      "warmup_partial_tokens_excluded": len(prepared) % block_size,
                      **snapshot(real, warm, current, len(original), bytes_per_token * block_size, block_size)})
        current = identities.blocks(following)
        real.update(current)
        steps.append({"case": row["case"], "step": position, "stage": "after_real_followup",
                      **snapshot(real, warm, current, len(following), bytes_per_token * block_size, block_size)})
    if not steps:
        raise ValueError("Session must include a continuation")
    ratios = [step for step in steps if step["warmup_only_to_current_real_ratio"] is not None]
    return {
        "tool_continuations": len(rows),
        "peak_warmup_only": max(steps, key=lambda x: x["warmup_only_blocks"]),
        "peak_relative_warmup_only": max(ratios, key=lambda x: x["warmup_only_to_current_real_ratio"]) if ratios else None,
        "terminal": steps[-1],
        "steps": steps,
    }


def load_evidence(directory=SESSION_COST, config_path=ROOT / "model-config.json"):
    raw_report = (directory / "results.json").read_bytes()
    if sha(raw_report) != REPORT_SHA256:
        raise ValueError("Session-cost report differs from the reviewed CPU record")
    report = json.loads(raw_report)
    decoded = {}
    for name in ("native-output.json.gz", "input.json.gz"):
        compressed = (directory / name).read_bytes()
        expected = report["raw_evidence"][name]
        if sha(compressed) != expected["sha256"]:
            raise ValueError(f"Compressed evidence changed: {name}")
        raw = gzip.decompress(compressed)
        if sha(raw) != expected["uncompressed_sha256"] or len(raw) != expected["uncompressed_bytes"]:
            raise ValueError(f"Decoded evidence changed: {name}")
        decoded[name] = json.loads(raw)
    native_rows = decoded["native-output.json.gz"]
    native = {row["case"]: row for row in native_rows}
    wanted = [row["case"] for row in report["transitions"] + report["constructed_growth"]]
    if len(native) != len(native_rows) or set(native) != set(wanted):
        raise ValueError("Native cases differ from the reviewed session set")
    if [row["name"] for row in decoded["input.json.gz"]["cases"]] != wanted:
        raise ValueError("Input cases differ from the reviewed session set")
    config_bytes = config_path.read_bytes()
    if sha(config_bytes) != CONFIG_SHA256:
        raise ValueError("Model configuration differs from the pinned official file")
    return report, native, json.loads(config_bytes)


def summarize(sessions):
    ratios = [s["terminal"]["warmup_only_to_current_real_ratio"] for s in sessions]
    peak_ratios = [s["peak_relative_warmup_only"]["warmup_only_to_current_real_ratio"] for s in sessions]
    return {
        "sessions": len(sessions),
        "terminal_warmup_only_blocks_sum_isolated_sessions": sum(s["terminal"]["warmup_only_blocks"] for s in sessions),
        "terminal_current_real_blocks_sum_isolated_sessions": sum(s["terminal"]["current_real_blocks"] for s in sessions),
        "terminal_warmup_to_current_ratio": {"mean": mean(ratios), "median": median(ratios), "min": min(ratios), "max": max(ratios)},
        "peak_warmup_to_current_ratio": {"mean": mean(peak_ratios), "median": median(peak_ratios), "min": min(peak_ratios), "max": max(peak_ratios)},
    }


def run(output):
    report, native, config = load_evidence()
    per_token = kv_bytes_per_token(config)
    recorded = []
    for info in report["sessions"]:
        rows = [row for row in report["transitions"] if row["run_id"] == info["run_id"]]
        recorded.append({"run_id": info["run_id"], "source_condition": info["source_condition"],
                         **session(rows, native, per_token)})
    constructed = []
    groups = dict.fromkeys(row["group"] for row in report["constructed_growth"])
    for group in groups:
        rows = [row for row in report["constructed_growth"] if row["group"] == group]
        constructed.append({"group": group, "placement": rows[0]["placement"],
                            "target_first_input_tokens": rows[0]["target_first_input_tokens"],
                            **session(rows, native, per_token)})
    result = {
        "evidence": "cpu_input_block_union_counterfactual",
        "gpu_measurements": False, "new_model_requests": 0,
        "model": "Qwen/Qwen3-8B", "model_revision": MODEL_REVISION,
        "model_config_url": CONFIG_URL, "model_config_sha256": CONFIG_SHA256,
        "script_sha256": sha(Path(__file__).read_bytes()), "session_cost_report_sha256": REPORT_SHA256,
        "native_raw_evidence": report["raw_evidence"], "source_revision": report["source_revision"],
        "kv": {"dtype": "bfloat16", "element_bytes": 2, "layers": config["num_hidden_layers"],
               "kv_heads": config["num_key_value_heads"], "head_dim": config["head_dim"],
               "bytes_per_token": per_token, "block_tokens": 16, "bytes_per_block": per_token * 16},
        "summary": summarize(recorded),
        "by_source_condition": {condition: summarize([s for s in recorded if s["source_condition"] == condition])
                                for condition in ("on", "off")},
        "recorded_sessions": recorded, "constructed_growth": constructed,
        "assumptions": [
            "Each session starts cold and retains every completed full input block with no eviction or cancellation.",
            "Every stock warmup completes before its following real request; source-off warmups are counterfactual.",
            "Block identity includes every preceding block; identical later text cannot merge across divergent prefixes.",
            "Real history includes each original and follow-up input when it occurs; future requests never enter early.",
            "BF16 K and V, 36 full-attention layers, 8 KV heads, head dimension 128, one unsharded GPU.",
        ],
        "limits": [
            "This is retained input KV content under stated assumptions, not observed occupancy or allocated GPU memory.",
            "Partial blocks, sampled decode tokens, allocator metadata, temporary workspace and preallocated unused cache are excluded.",
            "Terminal assistant warmups have no captured continuation and are excluded from this packet.",
            "Cross-session sharing is excluded; the sum across isolated sessions is not simultaneous shared-server occupancy.",
            "Constructed length controls repeat filler and can exceed the original server context limit.",
            "Physical residency, evictions, compute and latency remain unmeasured.",
        ],
    }
    output.mkdir(parents=True)
    (output / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new output directory")
    run(args.output)
