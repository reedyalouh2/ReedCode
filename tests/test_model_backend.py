import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

import httpx
from openai import AsyncOpenAI

from model_backend import create_response, response_termination


TOOLS = [{
    "type": "function", "name": "bash", "description": "Run a command",
    "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
    "strict": True,
}]


def completion(message, finish_reason="stop", usage=None):
    return {
        "id": "test-completion", "object": "chat.completion", "created": 1,
        "model": "test-model", "choices": [{
            "index": 0, "finish_reason": finish_reason, "message": message,
        }], "usage": usage,
    }


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_responses_history_is_forwarded_without_conversion(self):
        history = [NS(type="reasoning", encrypted_content="opaque"), NS(type="function_call")]
        result = object()
        client = NS(responses=NS(create=AsyncMock(return_value=result)))
        returned = await create_response(
            client, api="responses", model="m", instructions="work", input=history,
            tools=TOOLS, max_output_tokens=4096,
        )
        self.assertIs(returned, result)
        args = client.responses.create.call_args.kwargs
        self.assertIs(args["input"], history)
        self.assertIs(args["tools"], TOOLS)
        self.assertEqual(args["max_output_tokens"], 4096)

    async def test_chat_tool_round_trip_preserves_ids_reasoning_and_arguments(self):
        messages_sent = []
        assistant = {
            "role": "assistant", "content": "Inspecting the test.",
            "reasoning_content": "older parser reasoning", "reasoning": "current parser reasoning",
            "tool_calls": [{
                "id": "call-a", "type": "function",
                "function": {"name": "bash", "arguments": '{"command":"pytest -q"}'},
            }, {
                "id": "call-b", "type": "function",
                "function": {"name": "bash", "arguments": "not JSON"},
            }],
        }
        replies = iter([
            completion(assistant, "tool_calls", {
                "prompt_tokens": 120, "completion_tokens": 12, "total_tokens": 132,
                "prompt_tokens_details": {"cached_tokens": 80},
            }),
            completion({"role": "assistant", "content": "Done."}),
        ])

        def handle(request):
            self.assertEqual(request.url.path, "/v1/chat/completions")
            messages_sent.append(json.loads(request.content))
            return httpx.Response(200, json=next(replies))

        async with AsyncOpenAI(
            base_url="http://test.invalid/v1", api_key="test-only", max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        ) as client:
            history = [{"role": "user", "content": "Fix the tests."}]
            result = await create_response(
                client, api="chat", model="test-model", instructions="work",
                input=history, tools=TOOLS, max_output_tokens=4096,
            )
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.usage.input_tokens, 120)
            self.assertEqual(result.usage.input_tokens_details.cached_tokens, 80)
            self.assertEqual(result.usage.output_tokens, 12)
            calls = [item for item in result.output if item.type == "function_call"]
            self.assertEqual([item.call_id for item in calls], ["call-a", "call-b"])
            self.assertEqual(calls[1].arguments, "not JSON")
            history.extend(result.output)
            for call in calls:
                history.append({
                    "type": "function_call_output", "call_id": call.call_id, "output": "result",
                })
            final = await create_response(
                client, api="chat", model="test-model", instructions="work", input=history, tools=TOOLS,
            )
        self.assertEqual(messages_sent[0]["max_completion_tokens"], 4096)
        self.assertEqual(messages_sent[0]["tools"][0]["function"]["name"], "bash")
        replay = messages_sent[1]["messages"]
        self.assertEqual(replay[2], assistant)
        self.assertEqual(replay[3:], [
            {"role": "tool", "tool_call_id": "call-a", "content": "result"},
            {"role": "tool", "tool_call_id": "call-b", "content": "result"},
        ])
        self.assertEqual(final.output_text, "Done.")
        self.assertIsNone(final.usage.input_tokens)
        self.assertIsNone(final.usage.input_tokens_details.cached_tokens)

    async def test_truncated_generation_is_incomplete_and_missing_cache_is_unknown(self):
        body = completion({"role": "assistant", "content": "partial"}, "length", {
            "prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28,
        })
        async with AsyncOpenAI(
            base_url="http://test.invalid/v1", api_key="test-only", max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=body),
            )),
        ) as client:
            result = await create_response(
                client, api="chat", model="m", instructions="work", input=[], tools=TOOLS,
            )
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.finish_reason, "length")
        self.assertTrue(response_termination(result)["output_limit_hit"])
        self.assertEqual(result.usage.input_tokens, 20)
        self.assertIsNone(result.usage.input_tokens_details.cached_tokens)

    async def test_length_limit_with_partial_tool_call_preserves_usage(self):
        body = completion({"role": "assistant", "content": None, "tool_calls": [{
            "id": None, "type": "function",
            "function": {"name": "bash", "arguments": '{"command":'},
        }]}, "length", {"prompt_tokens": 20, "completion_tokens": 4096, "total_tokens": 4116})
        async with AsyncOpenAI(
            base_url="http://test.invalid/v1", api_key="test-only", max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=body),
            )),
        ) as client:
            result = await create_response(
                client, api="chat", model="m", instructions="work", input=[], tools=TOOLS,
            )
        self.assertTrue(response_termination(result)["output_limit_hit"])
        self.assertEqual(result.usage.output_tokens, 4096)
        self.assertFalse(any(item.type == "function_call" for item in result.output))

    def test_only_explicit_budget_exhaustion_is_a_limit_stop(self):
        for details in (NS(reason="max_output_tokens"), {"reason": "max_output_tokens"}):
            event = response_termination(NS(status="incomplete", incomplete_details=details))
            self.assertTrue(event["output_limit_hit"])
            self.assertEqual(event["incomplete_reason"], "max_output_tokens")
        for reason in ("content_filter", None):
            event = response_termination(NS(status="incomplete", incomplete_details=NS(reason=reason)))
            self.assertFalse(event["output_limit_hit"])

    async def test_invalid_api_or_foreign_history_fails_before_request(self):
        client = NS(chat=NS(completions=NS(create=AsyncMock())))
        for api, history in (("unknown", []), ("chat", [NS(type="reasoning")])):
            with self.assertRaises(ValueError):
                await create_response(
                    client, api=api, model="m", instructions="work", input=history, tools=TOOLS,
                )
        client.chat.completions.create.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
