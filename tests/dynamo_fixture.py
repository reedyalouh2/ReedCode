"""A local Chat transport for checking the runner. It performs no inference."""

import json

import httpx


class FixtureTransport:
    def __init__(self, finish_reason="stop"):
        self.requests = []
        self.finish_reason = finish_reason

    def handle(self, request):
        body = json.loads(request.content)
        self.requests.append((dict(request.headers), body))
        final = request.headers.get("X-Dynamo-Session-Final") == "true"
        text = "ready" if len(body["messages"]) == 1 else "done"
        identity = f"fixture-{len(self.requests)}"
        usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
        if final:
            return httpx.Response(200, json={
                "id": identity, "object": "chat.completion", "created": 1, "model": "fixture",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "."}}], "usage": usage})
        tool_turn = bool(body.get("tools")) and not any(m["role"] == "tool" for m in body["messages"])
        delta = {"tool_calls": [{"index": 0, "id": "read-1", "type": "function",
                                  "function": {"name": "bash", "arguments": '{"command":"pwd"}'}}]} if tool_turn else {"content": text}
        finish = "tool_calls" if tool_turn and self.finish_reason == "stop" else self.finish_reason
        chunks = [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
            {"choices": [], "usage": usage},
        ]
        data = "".join("data: " + json.dumps({"id": identity, "object": "chat.completion.chunk",
                                             "created": 1, "model": "fixture", **c}) + "\n\n" for c in chunks)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=data + "data: [DONE]\n\n")
