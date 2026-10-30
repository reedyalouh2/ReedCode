"""In-process response and tool fixtures; no model or container is run."""

import json
import time

import httpx


class ScriptedPeer:
    def __init__(
        self, budget, finishes=None, empty=False, extra_text="fresh", usage_offset=0
    ):
        self.budget = budget
        self.usage_offset = usage_offset
        self.finishes = finishes or []
        self.empty, self.extra_text = empty, extra_text
        self.chat_calls = self.preparation_calls = 0
        self.bodies = []

    def __call__(self, request):
        body = json.loads(request.content)
        self.bodies.append(body)
        if request.url.path == "/v1/completions":
            self.preparation_calls += 1
            events = [
                {"id": "scripted-preparation", "choices": [{"text": "discard"}]},
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": len(body["prompt"]),
                        "completion_tokens": 1,
                    },
                },
            ]
        elif request.url.path == "/v1/chat/completions":
            self.chat_calls += 1
            call = self.chat_calls
            finish = (
                self.finishes[call - 1] if call <= len(self.finishes) else "tool_calls"
            )
            delta = (
                {}
                if self.empty
                else {"reasoning_content": self.extra_text + f" reasoning {call}"}
            )
            if not self.empty:
                if finish == "stop":
                    delta["content"] = self.extra_text + f" answer {call}"
                else:
                    delta["tool_calls"] = [
                        {
                            "index": 0,
                            "id": f"call-{call}",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path":"/workspace/requests/utils.py"}',
                            },
                        }
                    ]
            events = [
                {
                    "id": f"scripted-chat-{call}",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                },
                {
                    "id": f"scripted-chat-{call}",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                },
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": self.budget.count(body) + self.usage_offset,
                        "completion_tokens": 12,
                    },
                },
            ]
        else:
            raise AssertionError("Unexpected scripted route")
        raw = (
            "".join("data: " + json.dumps(event) + "\n\n" for event in events)
            + "data: [DONE]\n\n"
        )
        return httpx.Response(200, content=raw.encode())


class ScriptedSandbox:
    def __init__(self, delay=0.02):
        self.calls, self.delay = [], delay

    def invoke(self, name, arguments, folder, timeout_s):
        self.calls.append((name, arguments))
        time.sleep(min(self.delay, timeout_s))
        return {
            "exit_code": 0,
            "stdout": f"scripted fresh tool result {len(self.calls)}",
            "stderr": "",
            "failure": None,
        }
