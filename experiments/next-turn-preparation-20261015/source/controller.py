"""Fresh coding sessions with one speculative ordinary-user prefix per response."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit
import uuid

import httpx

from http_capture import HttpCapture
from prepare import PinnedQwen
from support import Stream, TokenBudget, observation, SYSTEM, TOOLS

CONFIG = {
    "model": "Qwen/Qwen3-8B",
    "revision": "b968826d9c46dd6066d109eabc6255188de91218",
    "context_limit": 32768,
    "max_output_tokens": 8192,
    "calls_per_turn": 5,
    "session_seconds": 600,
    "model_seconds": 60,
    "tool_seconds": 60,
    "tool_output_chars": 20000,
    "temperature": 0.6,
    "top_p": 0.95,
    "seed": 20261015,
}


class Events:
    def __init__(self):
        self.rows = []

    def emit(self, kind, **fields):
        now = time.monotonic_ns()
        self.rows.append({"kind": kind, "monotonic_ns": now, **fields})
        return now

    async def save(self, path):
        rows = list(self.rows)
        await asyncio.to_thread(
            Path(path).write_text,
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        )


def body_for(history, cache_salt, config=CONFIG):
    return {
        "model": config["model"],
        "messages": deepcopy(history),
        "tools": deepcopy(TOOLS),
        "tool_choice": "auto",
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_completion_tokens": config["max_output_tokens"],
        "temperature": config["temperature"],
        "top_p": config["top_p"],
        "seed": config["seed"],
        "chat_template_kwargs": {"enable_thinking": True},
        "nvext": {
            "cache_salt": cache_salt,
            "extra_fields": ["completion_token_ids", "engine_data"],
        },
    }


class ChatPredictor:
    def __init__(self, directory):
        directory = Path(directory)
        self.renderer = PinnedQwen(
            directory / "tokenizer.json", directory / "tokenizer_config.json"
        )

    def __call__(self, snapshot):
        if set(snapshot) != {"request", "assistant"}:
            raise ValueError("Only a completed prior request and assistant are allowed")
        body = snapshot["request"]
        expected = body_for(body["messages"], body["nvext"]["cache_salt"])
        if body != expected:
            raise ValueError(
                "Request differs from the fixed thinking-enabled chat settings"
            )
        return self.renderer.predict(
            {
                "messages": deepcopy(body["messages"])
                + [deepcopy(snapshot["assistant"])],
                "tools": deepcopy(body["tools"]),
                "enable_thinking": True,
            },
            "user",
        )


def response_issue(response):
    if response.get("error") or not response.get("done_event"):
        return "model_error"
    finish = response.get("finish_reason")
    if finish == "length":
        return "output_limit"
    if finish not in ("stop", "tool_calls"):
        return "unsupported_finish_reason"
    message = response.get("message") or {}
    if not isinstance(message, dict) or any(
        message.get(key) is not None and not isinstance(message.get(key), str)
        for key in ("content", "reasoning", "reasoning_content", "refusal")
    ):
        return "unsupported_message"
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list) or any(not isinstance(call, dict) for call in calls):
        return "invalid_tool_calls"
    if (
        not calls
        and not (message.get("content") or "").strip()
        and not (message.get("refusal") or "").strip()
    ):
        return "empty_response"
    ids = [call.get("id") for call in calls]
    if any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(
        set(ids)
    ):
        return "invalid_tool_ids"
    if any(
        call.get("type") != "function" or not isinstance(call.get("function"), dict)
        for call in calls
    ):
        return "invalid_tool_calls"
    if finish == "tool_calls" and not calls:
        return "missing_tool_calls"
    return None


class ChatModel:
    def __init__(self, endpoint, events, client):
        address = urlsplit(endpoint)
        if (
            address.scheme != "http"
            or address.hostname not in {"127.0.0.1", "localhost", "::1"}
            or address.path != "/v1/chat/completions"
            or address.username
            or address.password
            or address.query
            or address.fragment
        ):
            raise ValueError("Use a credential-free loopback chat-completions endpoint")
        self.endpoint, self.events, self.client = endpoint, events, client
        self.records = []

    async def complete(self, body, session_id, deadline):
        request_id = str(uuid.uuid4())
        headers = {
            "X-Request-ID": request_id,
            "X-Dynamo-Session-ID": session_id,
            "X-Dynamo-Request-ID": request_id,
            "traceparent": f"00-{uuid.uuid4().hex}-{uuid.uuid4().hex[:16]}-01",
        }
        row = {
            "client_request_id": request_id,
            "session_id": session_id,
            "request": deepcopy(body),
            "headers": headers,
            "raw_lines": [],
        }
        self.records.append(row)
        row["started_monotonic_ns"] = self.events.emit(
            "model_start",
            session_id=session_id,
            client_request_id=request_id,
            headers=headers,
        )
        stream = Stream()
        try:
            async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
                async with self.client.stream(
                    "POST", self.endpoint, json=body, headers=headers
                ) as response:
                    row["http_status"] = response.status_code
                    row["response_headers"] = {
                        key: value
                        for key, value in response.headers.items()
                        if key.lower()
                        in {"x-request-id", "x-dynamo-request-id", "traceparent"}
                    }
                    response.raise_for_status()
                    data_lines = []
                    async for line in response.aiter_lines():
                        received = time.monotonic_ns()
                        row["raw_lines"].append(
                            {"monotonic_ns": received, "line": line}
                        )
                        if line.startswith("data:"):
                            data_lines.append(line[5:].lstrip())
                        elif not line and data_lines:
                            stream.add("\n".join(data_lines), received)
                            data_lines = []
                            if stream.done:
                                break
                    row.update(stream.result())
                    message = row["message"]
                    if "reasoning" in message:
                        if (
                            "reasoning_content" in message
                            and message["reasoning_content"] != message["reasoning"]
                        ):
                            raise ValueError("Conflicting reasoning fields")
                        message["reasoning_content"] = message.pop("reasoning")
        except asyncio.CancelledError:
            row["error"] = "CancelledError"
            raise
        except Exception as error:
            row["error"] = type(error).__name__ + ": " + str(error)
        finally:
            row["done_event"] = stream.done
            row["partial_message"] = deepcopy(stream.message)
            row["partial_tool_calls"] = deepcopy(stream.calls)
            row["ended_monotonic_ns"] = self.events.emit(
                "model_end",
                session_id=session_id,
                client_request_id=request_id,
                done_event=stream.done,
                error=row.get("error"),
            )
        return row


class RollingPreparation:
    def __init__(self, enabled, predict, transport, events, session_id):
        self.enabled, self.predict, self.transport = enabled, predict, transport
        self.events, self.session_id = events, session_id
        self.attempts, self.jobs, self.cpu_jobs = [], [], []
        self.active = None
        self.active_row = None

    def before_dispatch(self):
        now = self.events.emit("foreground_dispatch_intent", session_id=self.session_id)
        if self.attempts:
            self.attempts[-1]["next_dispatch_monotonic_ns"] = now
        if self.active and not self.active.done():
            self.active_row["cancel_requested_monotonic_ns"] = now
            self.active.cancel()
        return now

    def after_response(self, request, response, issue, deadline):
        row = {
            "id": "prepare-" + response["client_request_id"],
            "trigger_request_id": response["client_request_id"],
            "snapshot_ready_monotonic_ns": time.monotonic_ns(),
            "status": "off" if not self.enabled else "pending",
            "next_dispatch_monotonic_ns": None,
            "future_reuse": "unobserved",
        }
        self.attempts.append(row)
        if issue:
            row.update(status="ineligible", reason=issue)
            return
        if not self.enabled:
            return
        if self.active and not self.active.done():
            row.update(status="skipped_prior_cleanup_pending")
            return
        snapshot = {"request": request, "assistant": response["message"]}
        self.active = asyncio.create_task(self._prepare(row, snapshot, deadline))
        self.active_row = row

        def complete(job):
            if job.cancelled():
                row.update(
                    status="cancelled_before_start",
                    ended_monotonic_ns=time.monotonic_ns(),
                )
            else:
                job.exception()

        self.active.add_done_callback(complete)
        self.jobs.append(self.active)

    async def _prepare(self, row, snapshot, deadline):
        def prediction():
            row["cpu_started_monotonic_ns"] = time.monotonic_ns()
            try:
                return self.predict(deepcopy(snapshot))
            finally:
                row["cpu_finished_monotonic_ns"] = time.monotonic_ns()

        try:
            row["status"] = "predicting"
            cpu = asyncio.create_task(asyncio.to_thread(prediction))
            self.cpu_jobs.append(cpu)
            ids = await asyncio.shield(cpu)
            row["prediction_ids"] = ids
            if not ids:
                row["status"] = "declined"
                return
            row["status"] = "in_flight"
            row["preparation_dispatch_monotonic_ns"] = time.monotonic_ns()
            async with asyncio.timeout(
                max(0.001, min(60, deadline - time.monotonic()))
            ):
                row["observations"] = await self.transport(
                    row["id"], {"prompt": ids, "max_tokens": 1}
                )
            row["status"] = "completed"
        except asyncio.CancelledError:
            row["status"] = "cancelled"
        except Exception as error:
            row.update(status="error", error_type=type(error).__name__)
        finally:
            row["ended_monotonic_ns"] = time.monotonic_ns()

    async def drain(self, deadline):
        started = time.monotonic_ns()
        pending = [job for job in self.jobs if not job.done()]
        if pending:
            _, pending = await asyncio.wait(
                pending, timeout=max(0, min(10, deadline - time.monotonic()))
            )
        for job in pending:
            job.cancel()
        if pending:
            await asyncio.wait(pending, timeout=2)
        if self.cpu_jobs:
            await asyncio.wait(self.cpu_jobs, timeout=2)
        for job in [*self.jobs, *self.cpu_jobs]:
            if job.done() and not job.cancelled():
                job.exception()
        return {
            "started_monotonic_ns": started,
            "ended_monotonic_ns": time.monotonic_ns(),
            "pending_jobs": sum(not job.done() for job in self.jobs),
            "pending_cpu_jobs": sum(not job.done() for job in self.cpu_jobs),
        }


async def run_session(
    task,
    session_id,
    model,
    sandbox,
    budget,
    policy,
    events,
    tool_root,
    cache_salt,
    config=CONFIG,
):
    started = time.monotonic()
    deadline = started + config["session_seconds"]
    history = [{"role": "system", "content": SYSTEM}]
    result = {
        "session_id": session_id,
        "task_id": task["task_id"],
        "model_calls": 0,
        "tool_calls": 0,
        "turns": [],
        "outcome": "not_started",
    }
    events.emit("session_start", session_id=session_id, task_id=task["task_id"])
    try:
        for turn, instruction in enumerate(
            [task["initial_prompt"], *task["followups"]]
        ):
            history.append({"role": "user", "content": instruction})
            events.emit(
                "user_message", session_id=session_id, turn=turn, content=instruction
            )
            turn_result = {"turn": turn, "calls": 0, "outcome": "call_limit"}
            result["turns"].append(turn_result)
            for _ in range(config["calls_per_turn"]):
                if time.monotonic() >= deadline:
                    result["outcome"] = "session_timeout"
                    return result
                body = body_for(history, cache_salt, config)
                count = budget.count(body)
                if count + config["max_output_tokens"] > config["context_limit"]:
                    result["outcome"] = "context_budget_stop"
                    return result
                policy.before_dispatch()
                try:
                    response = await model.complete(
                        body,
                        session_id,
                        min(deadline, time.monotonic() + config["model_seconds"]),
                    )
                except asyncio.CancelledError:
                    result["model_calls"] += 1
                    turn_result["calls"] += 1
                    policy.after_response(
                        body, model.records[-1], "controller_cancelled", deadline
                    )
                    raise
                result["model_calls"] += 1
                turn_result["calls"] += 1
                issue = response_issue(response)
                if (
                    issue in (None, "empty_response")
                    and (response.get("usage") or {}).get("prompt_tokens") != count
                ):
                    issue = "renderer_count_mismatch"
                response["response_issue"] = issue
                policy.after_response(body, response, issue, deadline)
                if (
                    issue == "empty_response"
                    and response.get("finish_reason") == "stop"
                ):
                    history.append(deepcopy(response["message"]))
                    turn_result["outcome"] = "empty_response"
                    break
                if issue:
                    result["outcome"] = issue
                    return result
                message = response["message"]
                history.append(deepcopy(message))
                calls = message.get("tool_calls") or []
                if not calls:
                    turn_result["outcome"] = "assistant_finished"
                    break
                for tool in calls:
                    result["tool_calls"] += 1
                    events.emit("tool_start", session_id=session_id, tool_call=tool)
                    try:
                        arguments = json.loads(tool["function"]["arguments"])
                        remaining = min(
                            config["tool_seconds"], deadline - time.monotonic()
                        )
                        if remaining <= 0:
                            raise TimeoutError("session deadline before tool")
                        folder = (
                            Path(tool_root) / session_id / f"{result['tool_calls']:04d}"
                        )
                        tool_result = await asyncio.to_thread(
                            sandbox.invoke,
                            tool["function"]["name"],
                            arguments,
                            folder,
                            remaining,
                        )
                    except Exception as error:
                        tool_result = {
                            "exit_code": None,
                            "stdout": "",
                            "stderr": type(error).__name__ + ": " + str(error),
                            "failure": None
                            if isinstance(error, ValueError)
                            else "tool_error",
                        }
                    seen, truncated = observation(
                        tool_result, config["tool_output_chars"]
                    )
                    history.append(
                        {"role": "tool", "tool_call_id": tool["id"], "content": seen}
                    )
                    events.emit(
                        "tool_end",
                        session_id=session_id,
                        tool_call_id=tool["id"],
                        result=tool_result,
                        observation=seen,
                        truncated=truncated,
                    )
                    if tool_result.get("failure"):
                        result["outcome"] = tool_result["failure"]
                        return result
            events.emit("user_turn_end", session_id=session_id, **turn_result)
        if any(t["outcome"] == "empty_response" for t in result["turns"]):
            result["outcome"] = "finished_with_turn_failures"
        else:
            result["outcome"] = (
                "finished"
                if all(t["outcome"] == "assistant_finished" for t in result["turns"])
                else "turn_call_limit"
            )
        return result
    except asyncio.CancelledError:
        result["outcome"] = "controller_stop"
        raise
    except Exception as error:
        result.update(
            outcome="session_error", error=type(error).__name__ + ": " + str(error)
        )
        return result
    finally:
        result["foreground_elapsed_s"] = time.monotonic() - started
        result["terminal_preparation_drain"] = await policy.drain(deadline)
        result["elapsed_s"] = time.monotonic() - started
        result["history"] = history
        result["preparations"] = policy.attempts
        result["capture_flush"] = await policy.transport.close()
        destination = Path(tool_root).parent / "sessions" / (session_id + ".json")

        def persist():
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(result, indent=2) + "\n")

        await asyncio.to_thread(persist)
        events.emit(
            "session_end",
            session_id=session_id,
            outcome=result["outcome"],
            model_calls=result["model_calls"],
            tool_calls=result["tool_calls"],
        )
