"""Exercise a generated tool turn and inspect its actual captured warmup."""

import argparse
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from prepare import ROOT, json_bytes, sha


spec = importlib.util.spec_from_file_location("live_stock_probe", ROOT.parent / "stock_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
FILE_TEXT = "PROJECT_NAME=ReedCode\n"


def initial_request():
    return {"model": "Qwen/Qwen3-8B", "temperature": 0, "max_completion_tokens": 256,
        "stream": True, "stream_options": {"include_usage": True},
        "nvext": {"agent_hints": {"speculative_prefill": True}},
        "messages": [{"role": "system", "content": "Follow the user's instructions. The read_file tool reads a small fixture file."},
                     {"role": "user", "content": 'Call read_file exactly once with path "project.txt". After receiving its contents, reply with only the PROJECT_NAME value. Do not guess the contents.'}],
        "tools": [{"type": "function", "function": {"name": "read_file",
            "description": "Read a public fixture file by path.", "parameters": {"type": "object",
                "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}}}],
        "tool_choice": "auto"}


def following_request(request, response):
    calls = response["message"].get("tool_calls", [])
    if response["finish_reason"] != "tool_calls" or len(calls) != 1:
        raise ValueError("Expected one complete model-generated tool call")
    call = calls[0]
    if (call.get("type") != "function" or not call.get("id") or call["function"]["name"] != "read_file"
            or json.loads(call["function"]["arguments"]) != {"path": "project.txt"}):
        raise ValueError("Generated tool call differs from the fixed read_file fixture")
    result = deepcopy(request)
    result.pop("nvext")
    result["messages"].extend([deepcopy(response["message"]),
        {"role": "tool", "tool_call_id": call["id"], "content": FILE_TEXT}])
    return result


def send(client, url, request, request_id, session_id, output, turn, timeout):
    body = json_bytes(request)
    (output / f"request-{turn}.json").write_bytes(body)
    headers = {"Content-Type": "application/json", "Accept-Encoding": "identity",
               "X-Request-ID": request_id, "X-Dynamo-Session-ID": session_id}
    meta = {"request_id": request_id, "request_headers": headers, "started_unix": time.time(),
            "request_sha256": sha(body)}
    started = time.monotonic()
    raw = bytearray()
    try:
        with (output / f"response-{turn}.sse").open("wb") as saved:
            with client.stream("POST", url, content=body, headers=headers, timeout=timeout) as response:
                meta.update(status=response.status_code, response_headers=dict(response.headers))
                for part in response.iter_raw():
                    saved.write(part)
                    saved.flush()
                    raw.extend(part)
                    if time.monotonic() - started > timeout or len(raw) > 8 * 1024 * 1024:
                        raise TimeoutError("Smoke response exceeded its time or capture bound")
                response.raise_for_status()
        lines = raw.decode().splitlines()
        if not any(line.strip() == "data: [DONE]" for line in lines):
            raise ValueError("SSE response lacks its final marker")
        parsed = probe.parse_response(lines)
        (output / f"parsed-{turn}.json").write_bytes(json_bytes(parsed))
        return parsed, meta
    finally:
        meta.update(ended_unix=time.time(), wall_seconds=time.monotonic() - started,
                    response_sha256=sha(bytes(raw)))
        (output / f"http-{turn}.json").write_bytes(json_bytes(meta))


def run(args):
    request = initial_request()
    if not args.execute:
        print(json.dumps({"execute": False, "request": request, "tool_result": FILE_TEXT,
                          "wait_seconds": args.wait_seconds, "warmup_validation": "pending actual wire capture"}, indent=2))
        return
    parsed_url = urlsplit(args.base_url)
    if (parsed_url.scheme != "http" or parsed_url.hostname not in ("localhost", "127.0.0.1", "::1")
            or parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment):
        raise ValueError("Use a credential-free loopback HTTP base URL")
    args.output.mkdir(parents=True, exist_ok=False)
    prefix = f"hint-smoke-{args.variant}-{uuid4().hex}"
    manifest = {"status": "started", "variant": args.variant, "session_id": prefix,
                "started_unix": time.time(), "thinking": "disabled by the verified backend deployment",
                "script_sha256": sha(Path(__file__).read_bytes()),
                "parser_sha256": sha((ROOT.parent / "stock_probe.py").read_bytes()),
                "virtual_file": {"path": "project.txt", "contents": FILE_TEXT, "host_file_access": False},
                "wait_seconds": args.wait_seconds, "requests": [],
                "warmup_validation": "pending actual wire capture; delay alone proves no warmup"}
    deadline = time.monotonic() + args.max_seconds
    try:
        with httpx.Client(follow_redirects=False, trust_env=False) as client:
            for turn in range(2):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Smoke exceeded its total deadline")
                response, meta = send(client, args.base_url.rstrip("/") + "/chat/completions", request,
                                      prefix + f"-{turn}", prefix, args.output, turn, remaining)
                manifest["requests"].append(meta)
                if turn == 0:
                    if response["usage"].get("prompt_tokens_details", {}).get("cached_tokens") != 0:
                        raise ValueError("Initial generated-tool request did not report a cold cache")
                    request = following_request(request, response)
                    if time.monotonic() + args.wait_seconds >= deadline:
                        raise TimeoutError("Insufficient time remains for warmup wait and follow-up")
                    time.sleep(args.wait_seconds)
                elif response["message"].get("tool_calls") or response["finish_reason"] != "stop":
                    raise ValueError("Tool-result continuation did not finish normally")
        manifest.update(status="http_smoke_completed", actual_generated_tool_call=True,
                        final_answer_matches_fixture=response["message"].get("content", "").strip() == "ReedCode")
    except Exception as error:
        manifest.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        manifest["ended_unix"] = time.time()
        manifest["files"] = {p.name: sha(p.read_bytes()) for p in sorted(args.output.iterdir()) if p.is_file()}
        (args.output / "smoke.json").write_bytes(json_bytes(manifest))
    print(json.dumps({"status": manifest["status"], "output": str(args.output),
                      "warmup_validation": manifest["warmup_validation"]}, indent=2))


def check_wire(directory, wire_path):
    manifest = json.loads((directory / "smoke.json").read_text())
    if manifest["status"] != "http_smoke_completed":
        raise ValueError("HTTP smoke did not complete")
    for name, digest in manifest["files"].items():
        if sha((directory / name).read_bytes()) != digest:
            raise ValueError("Smoke evidence hash changed: " + name)
    wire = json.loads(wire_path.read_text())
    requests = [row for row in wire["requests"] if row.get("kind", "model") == "model"]
    real = []
    for http in manifest["requests"]:
        matches = [row for row in requests if row.get("http_link", {}).get("http_request_id") == http["request_id"]]
        if len(matches) != 1 or not matches[0].get("complete"):
            raise ValueError("HTTP smoke lacks a unique complete wire/log link")
        real.append(matches[0])
    lo, hi = int(real[0]["trace_headers"]["x-frontend-send-ts-ns"]), int(real[1]["trace_headers"]["x-frontend-send-ts-ns"])
    candidates = [row for row in requests if lo < int(row["trace_headers"]["x-frontend-send-ts-ns"]) < hi]
    if len(candidates) != 1 or not candidates[0].get("complete"):
        raise ValueError("Expected one complete internal warmup between the paired model requests")
    warmup = candidates[0]
    if warmup["request"].get("stop_conditions", {}).get("max_tokens") != 1:
        raise ValueError("The intervening request is not a one-token warmup")
    warmed, following = warmup["input_token_ids"], real[1]["input_token_ids"]
    common = next((i for i, (a, b) in enumerate(zip(warmed, following)) if a != b), min(len(warmed), len(following)))
    result = {"variant": manifest["variant"], "actual_generated_tool_call": True,
        "normal_runtime_ids": [row["request_id"] for row in real], "warmup_runtime_id": warmup["request_id"],
        "warmup_tokens": len(warmed), "following_tokens": len(following), "common_prefix_tokens": common,
        "first_divergence_index": common if common < len(warmed) else None,
        "warmup_is_exact_prefix": common == len(warmed),
        "prepared_full_block_tokens": len(warmed) // 16 * 16, "matched_full_block_tokens": common // 16 * 16,
        "wire_sha256": sha(wire_path.read_bytes()), "smoke_sha256": sha((directory / "smoke.json").read_bytes()),
        "scope": "Actual generated Chat tool turn and captured internal warmup; no CPU-rendered substitute."}
    (directory / "warmup-wire-check.json").write_bytes(json_bytes(result))
    if manifest["variant"] == "fixed" and not result["warmup_is_exact_prefix"]:
        raise ValueError("Fixed internal warmup diverges from the actual continuation; evidence preserved")
    print(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=("stock", "fixed"))
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-seconds", type=float, default=60)
    parser.add_argument("--wait-seconds", type=float, default=2)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--check-wire", type=Path)
    args = parser.parse_args()
    if not 0 < args.max_seconds <= 180 or not 0 <= args.wait_seconds <= 10:
        parser.error("max-seconds must be in (0, 180], wait-seconds in [0, 10]")
    if args.check_wire:
        if args.execute or args.output is None:
            parser.error("Offline wire check needs --output and no --execute")
        check_wire(args.output, args.check_wire)
    else:
        if args.execute and (args.output is None or args.variant is None):
            parser.error("Execution needs --variant and a fresh --output directory")
        run(args)
