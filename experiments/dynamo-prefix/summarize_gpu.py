"""Verify the saved cache probe and print its observed reuse."""

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent / "gpu-20260928"


def common_prefix(left, right):
    return next((i for i, pair in enumerate(zip(left, right)) if pair[0] != pair[1]),
                min(len(left), len(right)))


def summarize(directory=ROOT):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    for name, expected in manifest["sha256"].items():
        path = directory / name
        if path.resolve().parent != directory.resolve():
            raise ValueError("Unexpected record path")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Record changed: {name}")
    results = []
    for attempt in manifest["attempts"]:
        probe = json.loads((directory / attempt["probe"]).read_text())
        metrics = json.loads((directory / attempt["metrics"]).read_text())
        rows = []
        for case in probe["cases"]:
            data = case["inputs"]
            original = data["initial_prompt_token_ids"]
            following = data["actual_followup_token_ids"]
            candidate = data["prepared_token_ids"]
            shared = common_prefix(original, following)
            block_size = probe["block_size"]
            if shared >= len(original) or following[:len(candidate)] != candidate:
                raise ValueError("Invalid prepared-prefix or ordinary-decode bound")
            if len(candidate) % block_size:
                raise ValueError("Candidate does not end on a complete block")
            baseline = shared // block_size * block_size
            expected = baseline if case["condition"] == "off" else max(baseline, len(candidate))
            calls = case["calls"]
            stages = [c["stage"] for c in calls]
            required = ["initial", "followup"] if case["condition"] == "off" else ["initial", "prepare", "followup"]
            if stages != required:
                raise ValueError("Unexpected probe calls")
            for call in calls:
                if call["http_status"] != 200 or call["response"]["usage"]["prompt_tokens"] != len(call["request"]["prompt"]):
                    raise ValueError("Server did not process the submitted token IDs")
                if call["request"]["max_tokens"] != 1:
                    raise ValueError("Response budget changed")
            cached = lambda call: call["response"]["usage"]["prompt_tokens_details"]["cached_tokens"]
            if cached(calls[0]) != 0:
                raise ValueError("Initial prefix was already cached")
            observed = cached(calls[-1])
            if observed != expected:
                raise ValueError("Cache reuse differs from the exact prefix prediction")
            rows.append({"case": case["case"], "condition": case["condition"],
                         "cached_tokens": observed,
                         "cached_tokens_beyond_ordinary_prefix": observed - baseline,
                         "requests": len(calls),
                         "reported_completion_tokens": sum(c["response"]["usage"]["completion_tokens"] for c in calls)})
        count = sum(row["requests"] for row in rows)
        if metrics["valid_boundaries"]:
            finished = metrics["counter_deltas"]["requests_finished"]
            if finished["status"] != "ok" or finished["value"] != count:
                raise ValueError("Epoch backend count differs from the probe calls")
        results.append({"attempt": attempt["name"], "server_metrics_valid": metrics["valid_boundaries"],
                        "requests": count, "cases": rows})
    return {"evidence": "explicit_prefix_cache_probe", "workflow_speedup_measured": False,
            "attempts": results}


if __name__ == "__main__":
    print(json.dumps(summarize(), indent=2))
