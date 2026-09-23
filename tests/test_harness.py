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
        self.agent = harness.ReedCodeAgent(logs_dir=Path(self.tmp.name), model_name="test-model")
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

    async def test_incomplete_response_and_api_error_propagate_with_summary(self):
        with self.assertRaisesRegex(RuntimeError, "status: incomplete"):
            await self.run_with_responses([response(status="incomplete")])
        self.assertEqual(self.events()[-1]["stop_reason"], "error")
        with self.assertRaisesRegex(RuntimeError, "API unavailable"):
            await self.run_with_responses(RuntimeError("API unavailable"))
        self.assertEqual(self.events()[-1]["model_calls"], 0)

    async def test_cancellation_propagates_with_summary(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.run_with_responses(asyncio.CancelledError())
        self.assertEqual(self.events()[-1]["stop_reason"], "cancelled_or_task_timeout")


if __name__ == "__main__":
    unittest.main()
