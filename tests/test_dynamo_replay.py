import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionChunk
from harbor.models.agent.context import AgentContext

import chat_stream
from chat_stream import stream_completion
from dynamo_metrics import EpochMetrics
from dynamo_replay import capture_workload, run_epoch, validate_workload
from dynamo_report import differences, epoch_values, report_replay
from model_backend import create_response
from dynamo_support import DynamoConfig
from dynamo_fixture import FixtureTransport
import reedcode_harbor_agent as harness
from server_metrics import parse_metrics
from test_server_metrics import exposition

ROOT = Path(__file__).resolve().parents[1]


def chunk(delta=None, finish=None, usage=None):
    return ChatCompletionChunk.model_validate({
        "id": "test", "object": "chat.completion.chunk", "created": 1, "model": "m",
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}] if usage is None else [],
        "usage": usage,
    })


class Stream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for item in self.chunks:
            if isinstance(item, BaseException):
                raise item
            yield item

    async def close(self):
        self.closed = True


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_fragments_reasoning_usage_and_first_output(self):
        stream = Stream([
            chunk({"role": "assistant", "content": ""}),
            chunk({"reasoning_content": "inspect"}),
            chunk({"tool_calls": [{"index": 0, "id": "c", "type": "function",
                                    "function": {"name": "bash", "arguments": '{"command":'}}]}),
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"pwd"}'}}]}),
            chunk(finish="tool_calls"),
            chunk(usage={"prompt_tokens": 30, "completion_tokens": 6, "total_tokens": 36}),
        ])
        client = NS(chat=NS(completions=NS(create=AsyncMock(return_value=stream))))
        with patch.object(chat_stream.time, "perf_counter", side_effect=[10, 12, 15]):
            result, timing = await stream_completion(client, model="m", messages=[])
        self.assertEqual(timing["first_output_ms"], 2000)
        self.assertEqual(timing["stream_ms"], 5000)
        message = result.choices[0].message
        self.assertEqual(message.reasoning_content, "inspect")
        self.assertEqual(message.tool_calls[0].function.arguments, '{"command":"pwd"}')
        self.assertEqual(result.usage.completion_tokens, 6)
        self.assertTrue(stream.closed)

    async def test_cutoff_keeps_usage_but_no_executable_tools(self):
        stream = Stream([
            chunk({"tool_calls": [{"index": 0, "function": {"name": "bash", "arguments": '{"c'}}]}),
            chunk(finish="length"),
            chunk(usage={"prompt_tokens": 10, "completion_tokens": 64, "total_tokens": 74}),
        ])
        client = NS(chat=NS(completions=NS(create=AsyncMock(return_value=stream))))
        result = await create_response(client, api="chat", model="m", instructions="work", input=[],
                                       tools=[], stream=True, dynamo=DynamoConfig(True), session_id="s")
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.usage.output_tokens, 64)
        self.assertFalse(any(item.type == "function_call" for item in result.output))

    async def test_broken_and_cancelled_streams_close_and_keep_timing(self):
        for tail in ([], [httpx.ReadError("dropped")], [asyncio.CancelledError()]):
            stream = Stream([chunk({"content": "partial"}), *tail])
            client = NS(chat=NS(completions=NS(create=AsyncMock(return_value=stream))))
            with self.assertRaises(BaseException) as caught:
                await stream_completion(client, model="m", messages=[])
            self.assertIsNotNone(caught.exception.stream_timing["first_output_ms"])
            self.assertTrue(stream.closed)

    async def test_missing_usage_is_unknown(self):
        stream = Stream([chunk({"content": "done"}), chunk(finish="stop")])
        client = NS(chat=NS(completions=NS(create=AsyncMock(return_value=stream))))
        result, _ = await stream_completion(client, model="m", messages=[])
        self.assertIsNone(result.usage)


class ReplayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.workload = json.loads((ROOT / "experiments/dynamo/workload.json").read_text())

    async def test_harness_capture_replays_the_same_tool_conversation(self):
        fixture = FixtureTransport()

        def client_factory(**kwargs):
            return AsyncOpenAI(base_url="http://fixture/v1", api_key="fixture", **kwargs,
                               http_client=httpx.AsyncClient(transport=httpx.MockTransport(fixture.handle)))

        env = NS(exec=AsyncMock(return_value=NS(stdout="/app", stderr="", return_code=0)))
        with tempfile.TemporaryDirectory() as tmp:
            agent = harness.ReedCodeAgent(logs_dir=Path(tmp), model_name="fixture", settings=harness.Settings(
                model_api="chat", max_output_tokens=64, dynamo=DynamoConfig(True), capture_requests=True))
            agent.cwd = "/app"
            context = AgentContext()
            with patch.object(harness, "AsyncOpenAI", side_effect=client_factory), \
                 patch.dict(os.environ, {"OPENAI_BASE_URL": "http://fixture/v1"}):
                await agent.run("Inspect the working directory.", env, context)
            env.exec.assert_awaited_once()
            self.assertEqual(context.metadata["model_calls"], 2)
            workload = capture_workload(Path(tmp) / "reedcode_trace.jsonl")
            self.assertEqual(len(workload["workflows"][0]["turns"]), 2)
            replay_output = Path(tmp) / "replay"
            replay_output.mkdir()
            async with client_factory(max_retries=0) as client:
                result = await run_epoch(client, workload=workload, model="fixture", condition="off",
                                         copies=1, horizon_s=5, output=replay_output)
            self.assertTrue(all(c["recorded_message_match"] for c in result["workflows"][0]["calls"]))
        self.assertEqual(len(fixture.requests), 6)

    async def test_replay_keeps_prompts_fixed_and_cleans_up_each_session(self):
        fixture = FixtureTransport()
        results = []
        async with AsyncOpenAI(base_url="http://fixture/v1", api_key="fixture", max_retries=0,
                               http_client=httpx.AsyncClient(transport=httpx.MockTransport(fixture.handle))) as client:
            for condition in ("off", "on"):
                with tempfile.TemporaryDirectory() as tmp:
                    results.append(await run_epoch(client, workload=self.workload, model="fixture",
                                                    condition=condition, copies=2, horizon_s=5, output=Path(tmp)))
        for result in results:
            self.assertEqual([w["status"] for w in result["workflows"]], ["completed", "completed"])
            self.assertTrue(all(c["recorded_message_match"] for w in result["workflows"] for c in w["calls"]))
            self.assertEqual(epoch_values(result)["client_input_tokens"], 60)
        hashes = [[c["request_sha256"] for c in w["calls"]] for r in results for w in r["workflows"]]
        self.assertTrue(all(h == hashes[0] for h in hashes))
        sessions = {}
        for headers, body in fixture.requests:
            sessions.setdefault(headers["x-dynamo-session-id"], []).append((headers, body))
        self.assertEqual(len(sessions), 4)
        for requests in sessions.values():
            self.assertEqual(len(requests), 3)
            self.assertEqual(requests[-1][0]["x-dynamo-session-final"], "true")
            self.assertFalse(requests[-1][1]["nvext"]["agent_hints"]["speculative_prefill"])

    async def test_limit_and_timeout_receive_full_horizon(self):
        for reason, wait in (("length", 0), ("stop", 200)):
            fixture = FixtureTransport(reason)
            workload = copy.deepcopy(self.workload)
            workload["workflows"][0]["turns"][0]["wait_ms"] = wait
            async with AsyncOpenAI(base_url="http://fixture/v1", api_key="fixture", max_retries=0,
                                   http_client=httpx.AsyncClient(transport=httpx.MockTransport(fixture.handle))) as client:
                with tempfile.TemporaryDirectory() as tmp:
                    result = await run_epoch(client, workload=workload, model="fixture", condition="on",
                                             copies=1, horizon_s=0.1, output=Path(tmp))
            row = result["workflows"][0]
            self.assertEqual(row["capped_completion_ms"], 100)
            self.assertEqual(row["status"], "output_limit" if reason == "length" else "timeout")
            self.assertEqual(row["session_final"]["status"], "sent")

    def test_workload_rejects_nan_and_request_overrides(self):
        for change in (lambda w: w["workflows"][0].update(arrival_ms=float("nan")),
                       lambda w: w["workflows"][0]["turns"][0]["request"].update(extra_body={}),
                       lambda w: w["workflows"][0]["turns"][0]["request"].update(max_completion_tokens=0)):
            workload = copy.deepcopy(self.workload)
            change(workload)
            with self.assertRaises(ValueError):
                validate_workload(workload)

    def test_missing_pairs_do_not_turn_into_zero_gains(self):
        plan = [{"run_id": "a", "repeat": 1, "condition": "off"},
                {"run_id": "b", "repeat": 1, "condition": "on"}]
        result = differences([{**plan[0], "latency": 10}], plan, ["latency"])[0]
        self.assertEqual(result["pairs"], 0)
        self.assertIsNone(result["mean_on_minus_off"])
        self.assertEqual(len(result["missing_pairs"]), 1)

    def test_capture_keeps_tool_wait_and_rejects_incomplete_runs(self):
        events = [{"type": "run_config", "capture_requests": True, "model": "m"}]
        for number, turn in enumerate(self.workload["workflows"][0]["turns"], 1):
            events.extend([
                {"type": "request", "turn": number, "request": {"model": "m", **turn["request"]}},
                {"type": "inference", "turn": number, "response_status": "completed"},
                {"type": "assistant_message", "turn": number, "message": turn["expected_message"]},
            ])
        events.extend([{"type": "tool", "turn": 1, "duration_ms": 42},
                       {"type": "task_summary", "stop_reason": "no_tool_calls"}])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            path.write_text("\n".join(json.dumps(e) for e in events))
            captured = capture_workload(path)
            self.assertEqual(captured["workflows"][0]["turns"][1]["wait_ms"], 42)
            events[-1]["stop_reason"] = "max_output_tokens"
            path.write_text("\n".join(json.dumps(e) for e in events))
            with self.assertRaises(ValueError):
                capture_workload(path)


class EpochMetricTests(unittest.IsolatedAsyncioTestCase):
    async def test_counts_all_requests_and_waits_for_idle(self):
        step = 0

        def handle(request):
            return httpx.Response(200, text=exposition(step, kv=0.4))

        async with EpochMetrics("http://fixture/metrics", model_name="test-model", sample_interval=0.001,
                                transport=httpx.MockTransport(handle)) as metrics:
            step = 4
        report = metrics.result
        self.assertTrue(report["valid_boundaries"])
        self.assertEqual(report["counter_deltas"]["requests_finished"]["value"], 4)
        self.assertEqual(report["phase_totals"]["prefill"]["seconds"]["value"], 1)
        self.assertGreater(report["sampled_kv_fraction_seconds"], 0)
        self.assertEqual(report["scope"], "epoch")

    async def test_busy_boundaries_and_restarts_are_invalid(self):
        for busy, restart in ((True, False), (False, True)):
            after = False
            def handle(request):
                return httpx.Response(200, text=exposition(1 if after else 0,
                    running=int(busy), start=200 if after and restart else 100))
            async with EpochMetrics("http://fixture/metrics", sample_interval=0.001, drain_timeout=0.005,
                                    transport=httpx.MockTransport(handle)) as metrics:
                after = True
            self.assertFalse(metrics.result["valid_boundaries"])


if __name__ == "__main__":
    unittest.main()
