from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from openai import AsyncOpenAI
from harbor.models.agent.context import AgentContext

from dynamo_support import DynamoConfig
from model_backend import create_response
from output_policy import Settings
import reedcode_harbor_agent as harness
import run_dynamo as runner
import run_experiments


class DynamoWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_serving_metadata_changes_between_off_and_on(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={
                "id": "test", "object": "chat.completion", "created": 1, "model": "test-model",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "Done."}}],
            })

        async with AsyncOpenAI(
            base_url="http://dynamo.test/v1", api_key="test-only", max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        ) as client:
            for flag in (False, True, None):
                await create_response(
                    client, api="chat", model="test-model", instructions="work",
                    input=[{"role": "user", "content": "Fix the tests."}], tools=[],
                    max_output_tokens=4096,
                    dynamo=DynamoConfig(flag) if flag is not None else None,
                    session_id="session-1" if flag is not None else None,
                )
        bodies = [json.loads(r.content) for r in requests]
        for index, flag in enumerate((False, True)):
            self.assertEqual(requests[index].headers["X-Dynamo-Session-ID"], "session-1")
            self.assertEqual(bodies[index].pop("nvext"), {
                "agent_hints": {"speculative_prefill": flag},
            })
        self.assertNotIn("X-Dynamo-Session-ID", requests[2].headers)
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(bodies[1], bodies[2])

    async def test_invalid_api_and_missing_identity_fail_before_sending(self):
        client = NS(responses=NS(create=AsyncMock()), chat=NS(completions=NS(create=AsyncMock())))
        for api, session in (("responses", "s"), ("chat", None)):
            with self.assertRaises(ValueError):
                await create_response(client, api=api, model="m", instructions="work", input=[],
                                      tools=[], dynamo=DynamoConfig(True), session_id=session)
        client.responses.create.assert_not_awaited()
        client.chat.completions.create.assert_not_awaited()

    async def test_harness_reuses_session_and_records_sequential_tool_boundaries(self):
        call = NS(type="function_call", name="bash", arguments='{"command":"pwd"}', call_id="call-1")
        replies = [NS(status="completed", output=[call], output_text="", usage=None),
                   NS(status="completed", output=[], output_text="Done.", usage=None)]
        captured = []

        async def reply(*args, **kwargs):
            captured.append({**kwargs, "input": list(kwargs["input"])})
            return replies[len(captured) - 1]

        env = NS(exec=AsyncMock(return_value=NS(stdout="/app", stderr="", return_code=0)))
        with tempfile.TemporaryDirectory() as tmp:
            agent = harness.ReedCodeAgent(logs_dir=Path(tmp), model_name="m",
                                         settings=Settings(model_api="chat", dynamo=DynamoConfig(True)))
            agent.cwd = "/app"
            with patch.dict(os.environ, {"OPENAI_BASE_URL": "http://dynamo.test/v1"}), \
                 patch.object(harness, "AsyncOpenAI", return_value=AsyncMock()), \
                 patch.object(harness, "finish_session", AsyncMock(return_value={"status": "sent", "usage": None})), \
                 patch.object(harness, "create_response", side_effect=reply):
                await agent.run("task", env, AgentContext())
            events = [json.loads(line) for line in (Path(tmp) / "reedcode_trace.jsonl").read_text().splitlines()]
        session = captured[0]["session_id"]
        self.assertEqual(captured[1]["session_id"], session)
        self.assertTrue(all(e["session_id"] == session for e in events))
        times = [e["elapsed_ms"] for e in events]
        self.assertEqual(times, sorted(times))
        self.assertEqual([e["phase"] for e in events if e["type"] == "lifecycle"],
                         ["model_start", "tools_start", "tool_start", "tools_complete", "model_start"])
        self.assertEqual(events[0]["dynamo"]["hint_application"], "unverified")
        self.assertIn("EXIT CODE: 0", captured[1]["input"][-1]["output"])
        self.assertEqual(events[-1]["stop_reason"], "no_tool_calls")

    async def test_harness_requires_explicit_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = harness.ReedCodeAgent(logs_dir=Path(tmp), model_name="m",
                                         settings=Settings(model_api="chat", dynamo=DynamoConfig(False)))
            with patch.dict(os.environ, {}, clear=True), \
                 patch.object(harness, "AsyncOpenAI") as client, \
                 self.assertRaisesRegex(ValueError, "OPENAI_BASE_URL"):
                await agent.run("task", NS(), AgentContext())
            client.assert_not_called()


class DynamoPilotTests(unittest.TestCase):
    def test_explicit_settings_and_attribution_guard(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(Settings.from_env().dynamo)
            for value, flag in (("off", False), ("on", True)):
                os.environ.update(MODEL_API="chat", DYNAMO_SPECULATIVE_PREFILL=value)
                self.assertIs(Settings.from_env().dynamo.speculative_prefill, flag)
            os.environ["DYNAMO_SPECULATIVE_PREFILL"] = "false"
            with self.assertRaises(ValueError):
                Settings.from_env()
        with self.assertRaisesRegex(ValueError, "MODEL_API"):
            Settings(dynamo=DynamoConfig(True))
        with self.assertRaisesRegex(ValueError, "epoch-level"):
            Settings(model_api="chat", dynamo=DynamoConfig(False), server_metrics_url="http://m")
        for value in (0, 1, "false", None):
            with self.assertRaises(ValueError):
                DynamoConfig(value)
        for session in ("", "bad\nsession", "session with spaces", "é"):
            with self.assertRaises(ValueError):
                DynamoConfig(True).request_options(session)

    def test_schedule_and_environment_hold_retention_fixed(self):
        plan = runner.build_plan()
        self.assertEqual(plan, runner.build_plan())
        self.assertEqual(len(plan), 10)
        first_on = sum(plan[i]["condition"] == "on" for i in range(0, 10, 2))
        self.assertIn(first_on, (2, 3))
        for i in range(0, 10, 2):
            self.assertEqual({p["condition"] for p in plan[i:i+2]}, {"off", "on"})
            self.assertEqual({p["repeat"] for p in plan[i:i+2]}, {i // 2 + 1})
        with patch.dict(os.environ, {"MAX_TOOL_OUTPUT": "2000", "OUTPUT_POLICY": "head_tail",
                                     "VLLM_METRICS_URL": "http://old/metrics"}):
            off = runner.trial_env("http://dynamo/v1", 4096, "off")
            on = runner.trial_env("http://dynamo/v1", 4096, "on")
        self.assertNotIn("VLLM_METRICS_URL", off)
        self.assertEqual(off["MAX_TOOL_OUTPUT"], "20000")
        self.assertEqual(off["OUTPUT_POLICY"], "head")
        self.assertEqual(off.pop("DYNAMO_SPECULATIVE_PREFILL"), "off")
        self.assertEqual(on.pop("DYNAMO_SPECULATIVE_PREFILL"), "on")
        self.assertEqual(off, on)

    def test_plan_only_never_contacts_a_server_or_runs_harbor(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()), \
             patch.object(runner.subprocess, "run") as process:
            output = Path(tmp) / "study"
            result = runner.main(["--model", "test-model", "--output", str(output)])
            data = json.loads((output / "manifest.json").read_text())
        self.assertEqual(result, 0)
        self.assertEqual(data["state"], "planned")
        self.assertEqual(data["runs"], [])
        process.assert_not_called()

    def test_failed_oracle_stops_and_failed_verifier_is_retained(self):
        for reward in (0, 1):
            with self.subTest(oracle_reward=reward), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "evals/noisy-bugfix"
                source.mkdir(parents=True)
                (source / "task.toml").write_text("test")
                for name in ("run_dynamo.py", "run_experiments.py", "dynamo_support.py", "model_backend.py",
                             "output_policy.py", "reedcode_harbor_agent.py", "server_metrics.py", "chat_stream.py",
                             "dynamo_report.py", "reporting.py", "uv.lock"):
                    (root / name).write_text("test")
                deployment = root / "deployment.json"
                deployment.write_text(json.dumps({k: "test-only" for k in (
                    "dynamo_revision", "backend_version", "model_revision", "gpu", "dtype",
                    "context_limit", "cache_policy", "launch_command")}))
                conditions = []

                def fake_harbor(cmd, **kwargs):
                    oracle = cmd[cmd.index("--agent") + 1] == "oracle"
                    job = root / "jobs" / cmd[cmd.index("--job-name") + 1] / "trial"
                    job.mkdir(parents=True)
                    (job / "result.json").write_text(json.dumps({"verifier_result": {
                        "rewards": {"reward": reward if oracle else 0}}}))
                    if not oracle:
                        conditions.append(kwargs["env"]["DYNAMO_SPECULATIVE_PREFILL"])
                        (job / "reedcode_trace.jsonl").write_text(json.dumps({
                            "type": "task_summary", "stop_reason": "max_output_tokens",
                            "output_limit_hit": True}) + "\n")
                    return NS(returncode=0)

                output = root / "output"
                with patch.object(runner, "ROOT", root), patch.object(run_experiments, "ROOT", root), \
                     patch.object(runner.subprocess, "run", side_effect=fake_harbor), \
                     patch.object(runner.shutil, "which", return_value="harbor"), \
                     patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"}), redirect_stdout(io.StringIO()):
                    status = runner.main(["--model", "m", "--repeats", "1", "--output", str(output),
                                          "--execute", "--base-url", "http://dynamo.test/v1",
                                          "--deployment", str(deployment)])
                data = json.loads((output / "manifest.json").read_text())
                if reward == 0:
                    self.assertEqual(status, 1)
                    self.assertEqual(conditions, [])
                    self.assertEqual(data["state"], "preflight_failed")
                else:
                    self.assertEqual(status, 0)
                    self.assertEqual(set(conditions), {"off", "on"})
                    self.assertEqual([r["reward"] for r in data["runs"]], [0, 0])
                    self.assertEqual(data["state"], "finished")


if __name__ == "__main__":
    unittest.main()
