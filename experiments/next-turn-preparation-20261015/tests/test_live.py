import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import httpx

from controller import (
    CONFIG,
    ChatModel,
    ChatPredictor,
    Events,
    RollingPreparation,
    TokenBudget,
    body_for,
    response_issue,
    run_session,
)
from http_capture import HttpCapture
HERE = Path(__file__).resolve().parents[1] / "source"
from sandbox import DockerSandbox
from scripted import ScriptedPeer, ScriptedSandbox

TOKENIZER = Path(
    os.environ.get("REEDCODE_TOKENIZER_DIR", str(Path(__file__).resolve().parents[1] / ".reproduction/evidence/tokenizer"))
)
TASK = {
    "task_id": "scripted",
    "initial_prompt": "initial private task",
    "followups": ["unseen followup A", "unseen followup B"],
}


class RecordingTransport:
    def __init__(self, hold=False, cleanup=0):
        self.calls = []
        self.entered = asyncio.Event()
        self.cleaned = asyncio.Event()
        self.hold, self.cleanup = hold, cleanup

    async def __call__(self, label, body):
        self.calls.append((label, body))
        self.entered.set()
        try:
            if self.hold:
                await asyncio.Future()
            return {"actual_generated_tokens": 1}
        finally:
            await asyncio.sleep(self.cleanup)
            self.cleaned.set()

    async def close(self):
        return {}


def completed(i=1):
    return {
        "client_request_id": str(uuid.uuid4()),
        "done_event": True,
        "finish_reason": "stop",
        "message": {"role": "assistant", "content": f"fresh answer {i}"},
    }


class SessionTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.budget = TokenBudget(TOKENIZER)
        cls.predictor = ChatPredictor(TOKENIZER)

    async def session(
        self,
        *,
        enabled=True,
        finishes=None,
        empty=False,
        predict=None,
        delay=0.002,
        usage_offset=0,
    ):
        events = Events()
        peer = ScriptedPeer(
            self.budget, finishes=finishes, empty=empty, usage_offset=usage_offset
        )
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        client = httpx.AsyncClient(transport=httpx.MockTransport(peer))
        self.addAsyncCleanup(client.aclose)
        transport = HttpCapture(
            "http://127.0.0.1:18080",
            CONFIG["model"],
            folder / "prep",
            client=client,
            cache_salt="trial",
            dynamo_request_id=True,
        )
        policy = RollingPreparation(
            enabled, predict or self.predictor, transport, events, "session"
        )
        model = ChatModel("http://127.0.0.1:18080/v1/chat/completions", events, client)
        sandbox = ScriptedSandbox(delay)
        result = await run_session(
            TASK,
            "session",
            model,
            sandbox,
            self.budget,
            policy,
            events,
            folder / "tools",
            "trial",
        )
        return result, model, sandbox, policy, peer, events, folder

    async def test_fresh_fifteen_responses_have_fifteen_attempts_and_final_drain(self):
        result, model, sandbox, policy, peer, events, folder = await self.session()
        self.assertEqual(result["model_calls"], 15)
        self.assertEqual(result["tool_calls"], 15)
        self.assertEqual(len(policy.attempts), 15)
        self.assertEqual(result["outcome"], "turn_call_limit")
        self.assertEqual([x["calls"] for x in result["turns"]], [5, 5, 5])
        self.assertEqual(
            len([x for x in events.rows if x["kind"] == "user_message"]), 3
        )
        self.assertIsNone(policy.attempts[-1]["next_dispatch_monotonic_ns"])
        self.assertEqual(policy.attempts[-1]["future_reuse"], "unobserved")
        self.assertEqual(result["terminal_preparation_drain"]["pending_jobs"], 0)
        self.assertTrue((folder / "sessions/session.json").is_file())
        self.assertTrue(
            all(x["request"]["max_completion_tokens"] == 8192 for x in model.records)
        )
        self.assertTrue(
            all(
                x["request"]["chat_template_kwargs"]["enable_thinking"]
                for x in model.records
            )
        )
        self.assertEqual(peer.chat_calls, 15)
        self.assertTrue(
            all(x[1]["path"].startswith("/workspace/") for x in sandbox.calls)
        )

    async def test_off_never_predicts_or_sends_preparation(self):
        def forbidden(snapshot):
            raise AssertionError("off policy must not predict")

        result, _, _, policy, peer, _, _ = await self.session(
            enabled=False, predict=forbidden
        )
        self.assertEqual(result["model_calls"], 15)
        self.assertEqual(peer.preparation_calls, 0)
        self.assertEqual({x["status"] for x in policy.attempts}, {"off"})

    async def test_empty_response_is_retained_and_never_done(self):
        result, _, sandbox, policy, _, _, _ = await self.session(
            finishes=["stop"] * 3, empty=True
        )
        self.assertEqual(result["outcome"], "finished_with_turn_failures")
        self.assertEqual(result["model_calls"], 3)
        self.assertEqual(
            [t["outcome"] for t in result["turns"]], ["empty_response"] * 3
        )
        self.assertEqual(
            len([m for m in result["history"] if m["role"] == "assistant"]), 3
        )
        self.assertFalse(sandbox.calls)
        self.assertEqual(policy.attempts[0]["reason"], "empty_response")
        self.assertEqual(policy.attempts[0]["status"], "ineligible")

    async def test_empty_response_still_checks_renderer_count_before_continuing(self):
        result, model, sandbox, policy, peer, _, _ = await self.session(
            finishes=["stop"] * 3, empty=True, usage_offset=1
        )
        self.assertEqual(result["outcome"], "renderer_count_mismatch")
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(model.records[0]["response_issue"], "renderer_count_mismatch")
        self.assertFalse(sandbox.calls)
        self.assertEqual(peer.preparation_calls, 0)
        self.assertEqual(policy.attempts[0]["reason"], "renderer_count_mismatch")

    async def test_output_limit_never_executes_partial_tools_or_prepares(self):
        result, _, sandbox, policy, peer, _, _ = await self.session(finishes=["length"])
        self.assertEqual(result["outcome"], "output_limit")
        self.assertFalse(sandbox.calls)
        self.assertEqual(peer.preparation_calls, 0)
        self.assertEqual(policy.attempts[0]["reason"], "output_limit")

    async def test_predictor_only_sees_completed_state_and_present_user_messages(self):
        snapshots = []

        def predictor(snapshot):
            snapshots.append(deepcopy(snapshot))
            return [1] * 16

        await self.session(predict=predictor, delay=0.01)
        first = snapshots[0]
        self.assertEqual(set(first), {"request", "assistant"})
        self.assertNotIn("unseen followup", json.dumps(first))
        self.assertIn("fresh reasoning 1", json.dumps(first))
        self.assertNotIn("fresh reasoning 2", json.dumps(first))
        self.assertIn("unseen followup B", json.dumps(snapshots[-1]))

    async def test_http_ids_are_pinned_canonical_uuid_on_normal_calls(self):
        _, model, _, _, _, _, _ = await self.session(finishes=["stop"] * 3)
        for row in model.records:
            value = row["client_request_id"]
            self.assertEqual(str(uuid.UUID(value)), value)
            self.assertEqual(row["headers"]["X-Request-ID"], value)
            self.assertEqual(row["headers"]["X-Dynamo-Request-ID"], value)

    async def test_incomplete_stream_keeps_partial_response(self):
        def handler(request):
            event = {"choices": [{"index": 0, "delta": {"content": "partial"}}]}
            return httpx.Response(
                200, content=("data: " + json.dumps(event) + "\n\n").encode()
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = ChatModel("http://localhost/v1/chat/completions", Events(), client)
            result = await model.complete(
                body_for([], "salt"), "s", time.monotonic() + 1
            )
        self.assertEqual(response_issue(result), "model_error")
        self.assertEqual(result["partial_message"]["content"], "partial")
        self.assertFalse(result["done_event"])

    async def test_controller_cancel_keeps_started_request_and_ineligible_attempt(self):
        events, transport = Events(), RecordingTransport()
        started = asyncio.Event()

        class BlockedModel:
            def __init__(self):
                self.records = []

            async def complete(self, body, session_id, deadline):
                self.records.append(
                    {
                        "client_request_id": str(uuid.uuid4()),
                        "request": body,
                        "error": "CancelledError",
                        "done_event": False,
                    }
                )
                started.set()
                await asyncio.Future()

        model = BlockedModel()
        policy = RollingPreparation(
            True, lambda s: [1] * 16, transport, events, "cancel"
        )
        with tempfile.TemporaryDirectory() as directory:
            task = asyncio.create_task(
                run_session(
                    TASK,
                    "cancel",
                    model,
                    ScriptedSandbox(),
                    self.budget,
                    policy,
                    events,
                    Path(directory) / "tools",
                    "salt",
                )
            )
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            retained = json.loads(
                (Path(directory) / "sessions/cancel.json").read_text()
            )
        self.assertEqual(retained["outcome"], "controller_stop")
        self.assertEqual(retained["model_calls"], 1)
        self.assertEqual(retained["preparations"][0]["reason"], "controller_cancelled")
        self.assertFalse(transport.calls)


    async def test_fragmented_tools_and_reasoning_alias_are_accumulated(self):
        chunks = [
            {
                "delta": {
                    "reasoning": "think ",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "a",
                            "function": {"name": "read_", "arguments": '{"path":'},
                        }
                    ],
                }
            },
            {
                "delta": {
                    "reasoning": "more",
                    "tool_calls": [
                        {"index": 0, "function": {"name": "file", "arguments": '"x"}'}}
                    ],
                }
            },
            {"delta": {}, "finish_reason": "tool_calls"},
        ]
        data = (
            "".join(
                "data: " + json.dumps({"choices": [{"index": 0, **row}]}) + "\n\n"
                for row in chunks
            )
            + "data: [DONE]\n\n"
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=data))
        ) as client:
            result = await ChatModel(
                "http://localhost/v1/chat/completions", Events(), client
            ).complete(body_for([], "salt"), "s", time.monotonic() + 1)
        self.assertIsNone(response_issue(result))
        self.assertEqual(result["message"]["reasoning_content"], "think more")
        self.assertNotIn("reasoning", result["message"])
        self.assertEqual(
            result["message"]["tool_calls"][0]["function"],
            {"name": "read_file", "arguments": '{"path":"x"}'},
        )


class SchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_does_not_wait_for_transport_cleanup(self):
        events, transport = Events(), RecordingTransport(hold=True, cleanup=0.05)
        policy = RollingPreparation(True, lambda s: [1] * 16, transport, events, "s")
        response = completed()
        policy.after_response({}, response, None, time.monotonic() + 2)
        await asyncio.wait_for(transport.entered.wait(), 1)
        dispatch = policy.before_dispatch()
        self.assertFalse(transport.cleaned.is_set())
        # A real foreground request can begin here while transport cleanup is pending.
        foreground = events.emit("model_start")
        policy.after_response({}, completed(2), None, time.monotonic() + 2)
        self.assertEqual(policy.attempts[1]["status"], "skipped_prior_cleanup_pending")
        await policy.drain(time.monotonic() + 1)
        self.assertLessEqual(dispatch, foreground)
        self.assertLess(foreground, policy.attempts[0]["ended_monotonic_ns"])
        self.assertEqual(policy.attempts[0]["status"], "cancelled")
        self.assertTrue(transport.cleaned.is_set())

    async def test_canceled_cpu_work_is_recorded_but_never_sent(self):
        entered = threading.Event()

        def slow(snapshot):
            entered.set()
            time.sleep(0.07)
            return [1] * 16

        transport = RecordingTransport()
        policy = RollingPreparation(True, slow, transport, Events(), "s")
        policy.after_response({}, completed(), None, time.monotonic() + 2)
        while not entered.is_set():
            await asyncio.sleep(0.001)
        policy.before_dispatch()
        await policy.drain(time.monotonic() + 1)
        row = policy.attempts[0]
        self.assertEqual(row["status"], "cancelled")
        self.assertGreater(
            row["cpu_finished_monotonic_ns"], row["cancel_requested_monotonic_ns"]
        )
        self.assertFalse(transport.calls)
        self.assertNotIn("prediction_ids", row)

    async def test_cancellation_before_first_task_step_is_retained(self):
        transport = RecordingTransport()
        policy = RollingPreparation(True, lambda s: [1] * 16, transport, Events(), "s")
        policy.after_response({}, completed(), None, time.monotonic() + 1)
        policy.before_dispatch()
        await policy.drain(time.monotonic() + 1)
        self.assertEqual(policy.attempts[0]["status"], "cancelled_before_start")
        self.assertFalse(transport.calls)

    async def test_terminal_drain_respects_session_deadline_and_cancels(self):
        transport = RecordingTransport(hold=True)
        policy = RollingPreparation(True, lambda s: [1] * 16, transport, Events(), "s")
        policy.after_response({}, completed(), None, time.monotonic() + 1)
        await transport.entered.wait()
        start = time.monotonic()
        result = await policy.drain(time.monotonic() + 0.01)
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(result["pending_jobs"], 0)
        self.assertEqual(policy.attempts[0]["status"], "cancelled")


class PredictorAndReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.predictor = ChatPredictor(TOKENIZER)

    def test_future_field_and_changed_settings_fail_closed(self):
        snapshot = {
            "request": body_for([{"role": "user", "content": "known"}], "salt"),
            "assistant": {
                "role": "assistant",
                "reasoning_content": "reason",
                "content": "answer",
            },
        }
        ids = self.predictor(snapshot)
        self.assertEqual(len(ids) % 16, 0)
        self.assertTrue(ids)
        for field, value in [("future_request", {}), ("next_user_text", "secret")]:
            with self.assertRaises(ValueError):
                self.predictor({**snapshot, field: value})
        for mutate in [
            lambda b: b["chat_template_kwargs"].update(enable_thinking=False),
            lambda b: b.update(model="other"),
            lambda b: b.update(tools=[]),
            lambda b: b.update(max_completion_tokens=2048),
        ]:
            changed = deepcopy(snapshot)
            mutate(changed["request"])
            with self.assertRaises(ValueError):
                self.predictor(changed)

    def test_reasoning_only_reply_is_not_finished(self):
        response = completed()
        response["message"] = {
            "role": "assistant",
            "reasoning_content": "unfinished thinking",
        }
        self.assertEqual(response_issue(response), "empty_response")




class FileToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def invoke(self, path, mode="read", content=b""):
        return subprocess.run(
            [sys.executable, str(HERE / "file_tool.py"), str(self.root), path, mode],
            input=content,
            capture_output=True,
            timeout=3,
        )

    def test_large_write_uses_stdin_and_absolute_workspace_paths_work(self):
        content = b"x" * 95000
        result = self.invoke("/workspace/sub/file.py", "write", content)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.invoke("sub/file.py").stdout, content)
        self.assertEqual(self.invoke("/workspace/sub/file.py").stdout, content)

    def test_escape_symlink_and_hardlink_are_rejected(self):
        (self.root / "safe").write_text("original")
        (self.root / "link").symlink_to(self.root / "safe")
        (self.root / "hard").hardlink_to(self.root / "safe")
        for path in ["../bad", "/tmp/bad", "/workspace/../bad", "link", "hard"]:
            self.assertNotEqual(
                self.invoke(path, "write", b"changed").returncode, 0, path
            )
        self.assertEqual((self.root / "safe").read_text(), "original")

    def test_docker_argv_never_contains_file_content(self):
        sandbox = object.__new__(DockerSandbox)
        sandbox.container, sandbox.usable = "a" * 64, True
        sandbox.task = {}
        content = "payload-" * 14000
        observed = {}

        class Child:
            returncode = 0

            def poll(self):
                return 0

            def wait(self, timeout):
                return 0

        def popen(command, **kwargs):
            observed["command"] = command
            observed["stdin"] = kwargs["stdin"].read()
            return Child()

        with patch("sandbox.subprocess.Popen", popen):
            result = sandbox.invoke(
                "write_file",
                {"path": "/workspace/x", "content": content},
                self.root / "output",
                1,
            )
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(observed["stdin"], content.encode())
        self.assertNotIn(content, observed["command"])
        self.assertIn("-i", observed["command"])


if __name__ == "__main__":
    unittest.main()
