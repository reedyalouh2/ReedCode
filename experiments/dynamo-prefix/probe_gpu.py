"""Check whether an explicitly prepared prefix is reused by a live server."""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("prefix_probe_audit", ROOT / "audit.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def validated_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or any(character.isspace() for character in value)):
        raise ValueError("Use an HTTP(S) base URL without credentials, query, or fragment")
    _ = parsed.port
    return value.rstrip("/") + "/"


def probe_case(renderer, name: str, nonce: str, block_size: int) -> dict:
    source = "tool" if name == "tool_long" else name
    rows = json.loads((ROOT / f"fixtures/{source}-on.json").read_text())
    request, following = deepcopy(rows[0]["request"]), deepcopy(rows[1]["request"])
    assistant = deepcopy(rows[0]["response"]["choices"][0]["message"])
    provenance = {"source_capture": f"fixtures/{source}-on.json", "assistant_modified": False}
    if name == "tool_long":
        assistant["tool_calls"][0]["function"]["arguments"] = json.dumps({"value": "ready " + " ".join(["context"] * 512)})
        following["messages"][len(request["messages"])] = deepcopy(assistant)
        provenance.update({"assistant_modified": True, "synthetic_assistant": True,
                           "model_generated_long_arguments": False,
                           "change": "Append 512 copies of context to the known assistant's value argument",
                           "tool_was_executed": False})
    for item in (request, following):
        item["messages"][0]["content"] = f"{nonce}\n" + item["messages"][0]["content"]
    audit.check_followup_contract(request, assistant, following)
    initial = renderer.tokens(renderer.render(request, request["messages"], generation=True))
    actual = renderer.tokens(renderer.render(following, following["messages"], generation=True))
    candidate = renderer.candidate(request, assistant, block_size=block_size)["prepared_token_ids"]
    prefix = audit.common_prefix(initial, actual)
    if prefix >= len(initial):
        raise ValueError("This probe needs an input mismatch before ordinary sampled output")
    if actual[:len(candidate)] != candidate:
        raise ValueError("Prepared candidate is not an exact continuation prefix")
    return {"name": name, "nonce": nonce, "provenance": provenance, "initial_prompt_token_ids": initial,
            "actual_followup_token_ids": actual, "prepared_token_ids": candidate,
            "ordinary_matching_tokens": prefix,
            "ordinary_matching_full_block_tokens": prefix // block_size * block_size,
            "prepared_matching_full_block_tokens": len(candidate),
            "expected_additional_full_block_tokens": max(0, len(candidate) - prefix // block_size * block_size)}


def cached_tokens(response: dict) -> int:
    value = (response.get("usage") or {}).get("prompt_tokens_details") or {}
    value = value.get("cached_tokens")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("Server response is missing a nonnegative cached_tokens count")
    return value


async def completion(client, model: str, token_ids: list[int], label: str, calls: list[dict]) -> dict:
    body = {"model": model, "prompt": token_ids, "max_tokens": 1,
            "temperature": 0, "stream": False}
    row = {"stage": label, "request": body}
    calls.append(row)
    started = time.monotonic()
    try:
        response = await client.post("completions", json=body)
        row["http_status"] = response.status_code
        row["response"] = response.json()
        response.raise_for_status()
    finally:
        row["client_elapsed_ms"] = (time.monotonic() - started) * 1000
    prompt_tokens = row["response"].get("usage", {}).get("prompt_tokens")
    if prompt_tokens != len(token_ids):
        raise ValueError(f"{label}: server prompt token count differs from submitted token IDs")
    row["cached_tokens"] = cached_tokens(row["response"])
    return row


async def run_probe(client, renderer, *, block_size: int = 16, case_names=("tool", "text"),
                    nonce_factory=lambda: uuid4().hex, report: dict | None = None) -> dict:
    if block_size <= 0:
        raise ValueError("block_size must be positive")
    report = report if report is not None else {}
    report.update({"schema_version": 1, "experiment": "explicit_prefix_cache_probe",
                   "model": audit.MODEL, "model_revision": audit.REVISION,
                   "tokenizer_sha256": audit.TOKENIZER_SHA256,
                   "template_sha256": audit.TEMPLATE_SHA256,
                   "block_size": block_size, "server_speculative_prefill_enabled": False,
                   "server_side_patch_installed_by_probe": False,
                   "max_output_tokens_per_request": 1, "cases": [],
                   "timing_is_diagnostic_only": True, "status": "running"})
    first_blocks = set()
    for name in case_names:
        for condition in ("off", "explicit_preparation"):
            case = probe_case(renderer, name, nonce_factory(), block_size)
            first_block = tuple(case["initial_prompt_token_ids"][:block_size])
            if first_block in first_blocks:
                raise ValueError("Fresh condition does not have a unique first cache block")
            first_blocks.add(first_block)
            row = {"case": name, "condition": condition, "inputs": case, "calls": []}
            report["cases"].append(row)
            initial = await completion(client, audit.MODEL, case["initial_prompt_token_ids"], "initial", row["calls"])
            if initial["cached_tokens"] != 0:
                raise ValueError("Initial prompt was already cached; cold-prefix isolation failed")
            if condition == "explicit_preparation" and case["prepared_token_ids"]:
                await completion(client, audit.MODEL, case["prepared_token_ids"], "prepare", row["calls"])
            following = await completion(client, audit.MODEL, case["actual_followup_token_ids"], "followup", row["calls"])
            baseline = case["ordinary_matching_full_block_tokens"]
            expected = baseline if condition == "off" else max(baseline, case["prepared_matching_full_block_tokens"])
            row["expected_followup_cached_tokens"] = expected
            row["observed_followup_cached_tokens"] = following["cached_tokens"]
            row["matches_expected_cache_reuse"] = following["cached_tokens"] == expected
            row["observed_cached_tokens_beyond_ordinary_prefix"] = following["cached_tokens"] - baseline
            row["total_reported_prompt_tokens"] = sum(call["response"]["usage"]["prompt_tokens"] for call in row["calls"])
            row["total_reported_completion_tokens"] = sum(call["response"]["usage"]["completion_tokens"] for call in row["calls"])
    report["status"] = "complete"
    report["all_conditions_match_expected_cache_reuse"] = all(row["matches_expected_cache_reuse"] for row in report["cases"])
    return report


async def main_async(args) -> bool:
    import httpx

    renderer = audit.QwenRenderer(args.tokenizer)
    report = {"operator_declared_server_identity": args.server_identity,
              "server_identity_verified_by_probe": False}
    with args.output.open("x") as output:
        try:
            async with httpx.AsyncClient(base_url=validated_base_url(args.base_url), timeout=120,
                                         headers={"Authorization": "Bearer " + os.environ.get("REEDCODE_DYNAMO_API_KEY", "unused")}) as client:
                await run_probe(client, renderer, block_size=args.block_size,
                                case_names=args.cases, report=report)
            if not report["all_conditions_match_expected_cache_reuse"]:
                report["status"] = "cache_mismatch"
        except Exception as error:
            report["status"] = "failed"
            # HTTP header validation errors can include the authorization value.
            report["error"] = type(error).__name__
            raise RuntimeError("Probe failed: " + type(error).__name__) from None
        finally:
            output.write(json.dumps(report, indent=2) + "\n")
    return report["all_conditions_match_expected_cache_reuse"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:18000/v1")
    parser.add_argument("--server-identity", required=True, help="Deployment record or immutable image/model identity")
    parser.add_argument("--block-size", type=int, default=16)
    parser.add_argument("--cases", nargs="+", choices=("tool", "text", "tool_long"), default=["tool", "text"])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        validated_base_url(args.base_url)
        if args.block_size <= 0:
            raise ValueError("block_size must be positive")
        if not args.server_identity.strip():
            raise ValueError("server-identity must identify a deployment record")
        if args.output.exists():
            raise ValueError("Output exists; choose a new path to preserve the previous record")
    except ValueError as error:
        parser.error(str(error))
    raise SystemExit(0 if asyncio.run(main_async(args)) else 1)


if __name__ == "__main__":
    main()
