import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import reedcode_harbor_agent as harness
from harbor.models.agent.context import AgentContext


def response(output=(), status="completed", usage=True):
    return NS(status=status, output=list(output), output_text="done",
              usage=NS(input_tokens=10, output_tokens=2,
                       input_tokens_details=NS(cached_tokens=4)) if usage else None)


class HarnessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.agent = harness.ReedCodeAgent(logs_dir=Path(self.tmp.name), model_name="test-model",
                                           settings=harness.Settings())
        self.agent.cwd = "/app"
        self.env = NS(exec=AsyncMock(return_value=NS(stdout="ok", stderr="", return_code=0)))

    def test_absolute_paths_and_traversal(self):
        self.assertEqual(self.agent.safe_path("/app/src/main.py"), "/app/src/main.py")
        self.assertEqual(self.agent.safe_path("src/../main.py"), "/app/main.py")
        for invalid in ("../outside", "/app-other/file", "/etc/passwd", "", "a\0b", None):
            with self.assertRaises(ValueError):
                self.agent.safe_path(invalid)

    async def test_invalid_arguments_are_observations(self):
        for args in ("{broken", "[]", '{"command": 4}', "{}"):
            result = await self.agent.execute_tool(self.env, "bash", args)
            self.assertFalse(result.success)
            self.assertTrue(result.text.startswith("ERROR:"))
        self.env.exec.assert_not_awaited()

    async def test_timeout_recoverable_infrastructure_error_visible(self):
        for error in (TimeoutError(), RuntimeError("Command timed out after 120 seconds")):
            self.env.exec.side_effect = error
            result = await self.agent.execute_tool(self.env, "bash", {"command": "sleep 200"})
            self.assertFalse(result.success)
            self.assertIn("timed out", result.text)
        self.env.exec.side_effect = RuntimeError("Docker unavailable")
        with self.assertRaisesRegex(RuntimeError, "Docker unavailable"):
            await self.agent.execute_tool(self.env, "bash", {"command": "pwd"})

    async def test_nonzero_exit_preserves_status_and_caps_output(self):
        self.env.exec.return_value = NS(stdout="é" * 10, stderr="failure", return_code=1)
        self.agent.settings = harness.Settings(max_tool_output=3)
        result = await self.agent.execute_tool(self.env, "bash", {"command": "false"})
        self.assertFalse(result.success)
        self.assertEqual(result.text, "EXIT CODE: 1\nééé\n...[output truncated]")

    async def test_retention_telemetry_and_tail_reach_model(self):
        self.agent.settings = harness.Settings(max_tool_output=20, output_policy="head_tail")
        self.env.exec.return_value = NS(stdout="header" + "é" * 100 + "FAILED", stderr="", return_code=1)
        call = NS(type="function_call", name="bash", arguments='{"command":"pytest"}', call_id="c")
        context, client = await self.run_with_responses([response([call]), response()])
        event = next(e for e in self.events() if e["type"] == "tool")
        self.assertTrue(event["truncated"])
        self.assertEqual(event["original_output_chars"], 112)
        self.assertEqual(event["original_output_bytes"], 212)
        self.assertEqual(event["retained_output_chars"], 20)
        self.assertEqual(event["output_policy"], "head_tail")
        visible = client.responses.create.call_args.kwargs["input"][2]["output"]
        self.assertTrue(visible.endswith("FAILED"))
        self.assertTrue(visible.startswith("EXIT CODE: 1"))
        self.assertEqual(event["output_bytes"], len(visible.encode()))

    async def run_with_responses(self, responses):
        client = NS(responses=NS(create=AsyncMock(side_effect=responses)))
        manager = AsyncMock()
        manager.__aenter__.return_value = client
        context = AgentContext()
        with patch.object(harness, "AsyncOpenAI", return_value=manager):
            await self.agent.run("test task", self.env, context)
        return context, client

    def events(self):
        return [json.loads(line) for line in
                (Path(self.tmp.name) / "reedcode_trace.jsonl").read_text().splitlines()]

    async def test_tool_error_reenters_context_and_model_can_finish(self):
        call = NS(type="function_call", name="bash", arguments="not JSON", call_id="call-1")
        observed = []
        replies = iter([response([call]), response()])

        async def capture(**kwargs):
            observed.append(list(kwargs["input"]))
            return next(replies)

        context, _ = await self.run_with_responses(capture)
        self.assertIs(observed[1][1], call)
        self.assertEqual(observed[1][2]["call_id"], "call-1")
        self.assertIn("ERROR:", observed[1][2]["output"])
        self.assertEqual(context.metadata["tool_failures"], 1)
        self.assertEqual(context.metadata["stop_reason"], "no_tool_calls")
        self.assertNotIn("reward", context.metadata)

    async def test_turn_limit_is_not_completion(self):
        call = NS(type="function_call", name="bash", arguments='{"command":"pwd"}', call_id="c")
        self.agent.settings = harness.Settings(max_turns=1)
        context, _ = await self.run_with_responses([response([call])])
        self.assertFalse(context.metadata["agent_completed"])
        self.assertEqual(context.metadata["stop_reason"], "max_turns")

    async def test_missing_usage_stays_unknown(self):
        context, _ = await self.run_with_responses([response(usage=False)])
        self.assertIsNone(context.metadata["total_input_tokens"])
        self.assertIsNone(context.metadata["fresh_input_tokens"])

    async def test_output_limit_is_a_normal_stop_with_usage_and_no_tool_execution(self):
        # Even a complete-looking command in the cut-off response must not execute.
        call = NS(type="function_call", name="bash", arguments='{"command":"touch unexpected"}', call_id="c")
        reply = response([call], status="incomplete")
        reply.incomplete_details = NS(reason="max_output_tokens")
        context, client = await self.run_with_responses([reply])
        self.env.exec.assert_not_awaited()
        client.responses.create.assert_awaited_once()
        self.assertEqual(context.metadata["stop_reason"], "max_output_tokens")
        self.assertTrue(context.metadata["output_limit_hit"])
        self.assertFalse(context.metadata["agent_completed"])
        self.assertEqual(context.metadata["total_input_tokens"], 10)
        self.assertEqual(context.metadata["total_output_tokens"], 2)
        self.assertEqual(context.metadata["tool_calls"], 0)
        self.assertNotIn("reward", context.metadata)
        events = self.events()
        self.assertFalse(any(e["type"] in ("agent_error", "failed_inference_metrics") for e in events))
        event = next(e for e in events if e["type"] == "inference")
        self.assertTrue(event["output_limit_hit"])
        self.assertEqual(event["incomplete_reason"], "max_output_tokens")

    async def test_chat_length_after_tool_execution_keeps_all_call_totals(self):
        call = NS(type="function_call", name="bash", arguments='{"command":"pwd"}', call_id="c")
        limited = response(status="incomplete")
        limited.finish_reason = "length"
        context, _ = await self.run_with_responses([response([call]), limited])
        self.env.exec.assert_awaited_once()
        self.assertEqual(context.metadata["model_calls"], 2)
        self.assertEqual(context.metadata["tool_calls"], 1)
        self.assertEqual(context.metadata["total_input_tokens"], 20)
        self.assertEqual(context.metadata["total_output_tokens"], 4)
        self.assertTrue(context.metadata["output_limit_hit"])
        event = [e for e in self.events() if e["type"] == "inference"][-1]
        self.assertEqual(event["finish_reason"], "length")

    async def test_incomplete_response_and_api_error_propagate_with_summary(self):
        with self.assertRaisesRegex(RuntimeError, "status: incomplete"):
            await self.run_with_responses([response(status="incomplete")])
        self.assertEqual(self.events()[-1]["stop_reason"], "error")
        self.assertIsNone(self.events()[-1]["output_limit_hit"])
        with self.assertRaisesRegex(RuntimeError, "API unavailable"):
            await self.run_with_responses(RuntimeError("API unavailable"))
        self.assertEqual(self.events()[-1]["model_calls"], 0)

    async def test_cancellation_propagates_with_summary(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.run_with_responses(asyncio.CancelledError())
        self.assertEqual(self.events()[-1]["stop_reason"], "cancelled_or_task_timeout")

    async def test_metrics_boundaries_are_logged_but_excluded_from_api_latency(self):
        clock = [0.0]

        class Metrics:
            def __init__(self, *args, **kwargs):
                self.result = {"status": "test", "scope": "server_window"}

            async def __aenter__(self):
                clock[0] += 10
                return self

            async def __aexit__(self, *args):
                clock[0] += 10

        async def reply(**kwargs):
            clock[0] += 1
            return response()

        self.agent.settings = harness.Settings(server_metrics_url="http://localhost/metrics")
        with patch.object(harness, "ServerMetricsWindow", Metrics), \
             patch.object(harness.time, "perf_counter", side_effect=lambda: clock[0]):
            context, _ = await self.run_with_responses(reply)
        event = next(e for e in self.events() if e["type"] == "inference")
        self.assertEqual(event["latency_ms"], 1000)
        self.assertEqual(event["server_metrics"]["scope"], "server_window")
        self.assertEqual(context.metadata["wall_time_ms"], 21000)


if __name__ == "__main__":
    unittest.main()
