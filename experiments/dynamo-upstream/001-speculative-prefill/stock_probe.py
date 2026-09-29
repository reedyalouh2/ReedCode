"""Send two Chat requests using only Dynamo's stock speculative-prefill hint."""

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time
from uuid import uuid4
from urllib.parse import urlsplit

import httpx


def request_pair_start(case, enabled, nonce):
    request = {"model": "Qwen/Qwen3-8B", "temperature": 0, "max_completion_tokens": 128,
               "stream": True, "stream_options": {"include_usage": True},
               "nvext": {"agent_hints": {"speculative_prefill": enabled}},
               "messages": [{"role": "system", "content": f"{nonce}. Follow the user instruction."},
                            {"role": "user", "content": "Call record_value exactly once with value ready. Do not answer in prose."
                             if case == "tool" else "Reply with exactly ready."}]}
    if case == "tool":
        request.update(tool_choice="auto", tools=[{"type": "function", "function": {
            "name": "record_value", "description": "Record a short string.", "parameters": {
                "type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}}}])
    return request


def parse_response(lines):
    message, calls, usage, ident, finish = {"role": "assistant"}, {}, None, None, None
    for line in lines:
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            continue
        chunk = json.loads(data)
        ident = chunk.get("id") or ident
        usage = chunk.get("usage") or usage
        for choice in chunk.get("choices", []):
            if choice["index"] != 0:
                raise ValueError("This probe expects one choice")
            delta = choice.get("delta", {})
            for field in ("content", "reasoning_content", "reasoning"):
                if delta.get(field) is not None:
                    message[field] = message.get(field, "") + delta[field]
            for call in delta.get("tool_calls", []):
                part = calls.setdefault(call["index"], {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if call.get("id"):
                    if part["id"] and part["id"] != call["id"]:
                        raise ValueError("Conflicting tool-call IDs")
                    part["id"] = call["id"]
                for key in ("name", "arguments"):
                    part["function"][key] += call.get("function", {}).get(key) or ""
            finish = choice.get("finish_reason") or finish
    if finish not in ("stop", "tool_calls") or usage is None or ident is None:
        raise ValueError("Incomplete response or missing usage")
    if calls:
        message["tool_calls"] = [calls[index] for index in sorted(calls)]
    return {"id": ident, "message": message, "finish_reason": finish, "usage": usage}


def followup(request, response, case):
    following = deepcopy(request)
    following["nvext"]["agent_hints"]["speculative_prefill"] = False
    following["messages"].append(response["message"])
    calls = response["message"].get("tool_calls", [])
    if case == "tool":
        if len(calls) != 1 or calls[0]["function"]["name"] != "record_value":
            raise ValueError("Expected one record_value call")
        if json.loads(calls[0]["function"]["arguments"]) != {"value": "ready"} or not calls[0].get("id"):
            raise ValueError("Unexpected record_value arguments or missing call ID")
        following["messages"].append({"role": "tool", "tool_call_id": calls[0]["id"],
                                     "content": "Recorded successfully. Now reply with exactly done."})
    else:
        if calls:
            raise ValueError("Unexpected tool call in the text case")
        following["messages"].append({"role": "user", "content": "Now reply with exactly done."})
    return following


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--case", choices=("text", "tool"), required=True)
    parser.add_argument("--condition", choices=("off", "on"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--deployment", type=Path, required=True, help="Verified server versions, image digest, model revision, and launch commands")
    parser.add_argument("--api-key-file", type=Path)
    args = parser.parse_args()
    url = urlsplit(args.base_url)
    if (url.scheme not in ("http", "https") or not url.hostname or url.username or url.password
            or url.query or url.fragment):
        parser.error("Use a plain HTTP(S) base URL without credentials, query, or fragment")
    if args.output.exists():
        parser.error("Choose a new output directory")
    args.output.mkdir(parents=True)
    deployment = json.loads(args.deployment.read_text())
    for field in ("dynamo_version", "container_image", "model_revision", "launch_command"):
        if not deployment.get(field):
            parser.error(f"Deployment record lacks {field}")
    headers = {"X-Dynamo-Session-ID": "stock-probe-" + uuid4().hex}
    (args.output / "manifest.json").write_text(json.dumps({
        "case": args.case, "condition": args.condition, "base_url": args.base_url,
        "session_id": headers["X-Dynamo-Session-ID"], "deployment": deployment,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "deployment_independently_verified_by_client": False,
        "followup_hint": False, "wait_seconds": 2,
    }, indent=2) + "\n")
    if args.api_key_file:
        headers["Authorization"] = "Bearer " + args.api_key_file.read_text().strip()
    request = request_pair_start(args.case, args.condition == "on", uuid4().hex)
    with httpx.Client(timeout=90, follow_redirects=False) as client:
        for turn in range(2):
            (args.output / f"request-{turn}.json").write_text(json.dumps(request, indent=2) + "\n")
            # Keep the response even when parsing or validation fails.
            lines = []
            with (args.output / f"response-{turn}.sse").open("w") as saved:
                with client.stream("POST", args.base_url.rstrip("/") + "/chat/completions",
                                   json=request, headers=headers) as result:
                    result.raise_for_status()
                    for line in result.iter_lines():
                        saved.write(line + "\n")
                        lines.append(line)
            response = parse_response(lines)
            (args.output / f"parsed-{turn}.json").write_text(json.dumps(response, indent=2) + "\n")
            if turn == 0:
                if response["usage"].get("prompt_tokens_details", {}).get("cached_tokens") != 0:
                    raise ValueError("Initial request did not report a cold prefix")
                request = followup(request, response, args.case)
                time.sleep(2)
    print("Saved two stock Chat requests. Check server logs for preparation completion and token hashes.")


if __name__ == "__main__":
    main()
