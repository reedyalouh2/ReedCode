from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
import json
import os
import posixpath
import shlex
import time
from uuid import uuid4

from openai import AsyncOpenAI

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from output_policy import Settings, ToolObservation, observation
from model_backend import create_response, response_termination
from server_metrics import ServerMetricsWindow
from dynamo_support import finish_session


# Open each component without following links; checking realpath before opening races.
FILE_TOOL_SCRIPT = """
import base64
import os
import shutil
import stat
import sys
from contextlib import ExitStack

root, relative, operation = sys.argv[1:4]
parts = relative.split('/')
if any(part in ('', '.', '..') for part in parts):
    raise SystemExit('ERROR: expected a file inside the workspace')
try:
    with ExitStack() as stack:
        def hold(fd):
            stack.callback(os.close, fd)
            return fd

        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        parent = hold(os.open(root, directory_flags))
        for part in parts[:-1]:
            if operation == 'write':
                try:
                    os.mkdir(part, dir_fd=parent)
                except FileExistsError:
                    pass
            parent = hold(os.open(part, directory_flags, dir_fd=parent))
        flags = os.O_NOFOLLOW | os.O_NONBLOCK
        flags |= os.O_WRONLY | os.O_CREAT if operation == 'write' else os.O_RDONLY
        fd = hold(os.open(parts[-1], flags, 0o666, dir_fd=parent))
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError('file tools require a regular file')
        if operation == 'write':
            if metadata.st_nlink != 1:
                raise ValueError('write_file refuses files with multiple hard links')
            content = base64.b64decode(sys.argv[4], validate=True)
            os.ftruncate(fd, 0)
            with os.fdopen(os.dup(fd), 'wb') as file:
                file.write(content)
        else:
            with os.fdopen(os.dup(fd), 'rb') as file:
                shutil.copyfileobj(file, sys.stdout.buffer)
except (OSError, ValueError) as error:
    raise SystemExit('ERROR: ' + str(error))
"""


TOOLS = [
    {
        "type": "function",
        "name": "read_file",
        "description": "Read a text file in the coding workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "write_file",
        "description": "Write complete contents to a file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "bash",
        "description": "Run a shell command in the coding workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


def add_usage(total, value):
    return None if total is None or value is None else total + value


class ReedCodeAgent(BaseAgent):
    def __init__(self, *args, settings: Settings | None = None, **kwargs):
        self.settings = settings if settings is not None else Settings.from_env()
        super().__init__(*args, **kwargs)

    @staticmethod
    def name() -> str:
        return "reedcode"

    def version(self) -> str:
        return "0.4.2"

    async def setup(self, environment: BaseEnvironment) -> None:
        result = await environment.exec(
            command="pwd",
            timeout_sec=self.settings.tool_timeout,
        )

        cwd = (result.stdout or "").strip()

        if result.return_code != 0 or not posixpath.isabs(cwd):
            raise RuntimeError(
                "Could not determine the container working directory"
            )

        self.cwd = posixpath.normpath(cwd)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        if not self.model_name:
            raise ValueError("A model must be supplied with --model")
        if self.settings.dynamo is not None and not os.getenv("OPENAI_BASE_URL"):
            raise ValueError("Set OPENAI_BASE_URL to the Dynamo frontend before enabling hints")

        history = [{"role": "user", "content": instruction}]
        trace_path = self.logs_dir / "reedcode_trace.jsonl"
        started = time.perf_counter()
        session_id = f"reedcode-{uuid4().hex}" if self.settings.dynamo is not None else None

        total_input = total_cached = total_output = 0
        model_calls = tool_calls_total = tool_failures = 0
        model_ms = tool_ms_total = 0.0
        stop_reason = "error"

        def log(event: dict) -> None:
            if session_id is not None:
                event = {"session_id": session_id,
                         "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                         **event}
            with trace_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")

        def boundary(phase: str, **fields) -> None:
            if session_id is not None:
                log({"type": "lifecycle", "phase": phase, **fields})

        log({
            "type": "run_config",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "agent_version": self.version(),
            "model": self.model_name,
            "max_turns": self.settings.max_turns,
            "max_tool_output_chars": self.settings.max_tool_output,
            "output_policy": self.settings.output_policy,
            "tool_timeout_sec": self.settings.tool_timeout,
            "model_api": self.settings.model_api,
            "max_output_tokens": self.settings.max_output_tokens,
            "server_metrics_enabled": bool(self.settings.server_metrics_url),
            "metrics_sample_interval_sec": self.settings.metrics_sample_interval,
            "sdk_max_retries": 0,
            "capture_requests": self.settings.capture_requests,
            "dynamo": None if self.settings.dynamo is None else {
                "speculative_prefill": self.settings.dynamo.speculative_prefill,
                "hint_application": "unverified",
            },
        })

        try:
            async with AsyncOpenAI(max_retries=0) as client:
                for turn in range(1, self.settings.max_turns + 1):
                    boundary("model_start", turn=turn)
                    metrics = ServerMetricsWindow(
                        self.settings.server_metrics_url,
                        model_name=self.model_name,
                        sample_interval=self.settings.metrics_sample_interval,
                    )
                    try:
                        async with metrics:
                            start = time.perf_counter()
                            response = await create_response(
                                client,
                                api=self.settings.model_api,
                                model=self.model_name,
                                instructions=(
                                    "You are a coding agent working inside a repository. "
                                    "Inspect files, run commands, modify files when needed, "
                                    "and verify your work before finishing."
                                ),
                                input=history,
                                tools=TOOLS,
                                max_output_tokens=self.settings.max_output_tokens,
                                dynamo=self.settings.dynamo,
                                session_id=session_id,
                                stream=self.settings.dynamo is not None,
                                capture=(lambda request: log({"type": "request", "turn": turn,
                                                              "request": request}))
                                if self.settings.capture_requests else None,
                            )
                            latency_ms = (time.perf_counter() - start) * 1000
                    except BaseException as exc:
                        if session_id is not None:
                            log({"type": "failed_inference", "turn": turn,
                                 "error_type": type(exc).__name__,
                                 "stream_timing": getattr(exc, "stream_timing", None)})
                        if self.settings.server_metrics_url:
                            log({"type": "failed_inference_metrics", "turn": turn,
                                 "server_metrics": metrics.result})
                        raise
                    model_calls += 1
                    model_ms += latency_ms

                    usage = response.usage
                    input_tokens = getattr(usage, "input_tokens", None)
                    output_tokens = getattr(usage, "output_tokens", None)
                    details = getattr(usage, "input_tokens_details", None)
                    cached_tokens = getattr(details, "cached_tokens", None)

                    total_input = add_usage(total_input, input_tokens)
                    total_cached = add_usage(total_cached, cached_tokens)
                    total_output = add_usage(total_output, output_tokens)

                    tool_calls = [
                        item
                        for item in response.output
                        if item.type == "function_call"
                    ]
                    termination = response_termination(response)

                    log({
                        "type": "inference",
                        "turn": turn,
                        "latency_ms": round(latency_ms, 2),
                        "input_tokens": input_tokens,
                        "cached_input_tokens": cached_tokens,
                        "output_tokens": output_tokens,
                        "tool_calls": len(tool_calls),
                        "served_model": getattr(response, "model", None),
                        "server_metrics": metrics.result,
                        "completion_id": getattr(response, "completion_id", None),
                        "stream_timing": getattr(response, "stream_timing", None),
                        **termination,
                    })
                    if self.settings.capture_requests:
                        for item in response.output:
                            if item.type == "chat_message":
                                log({"type": "assistant_message", "turn": turn, "message": item.message})

                    context.n_input_tokens = total_input
                    context.n_cache_tokens = total_cached
                    context.n_output_tokens = total_output

                    if termination["output_limit_hit"]:
                        stop_reason = "max_output_tokens"
                        break

                    if response.status != "completed":
                        raise RuntimeError(
                            f"Model response status: {response.status}"
                        )

                    # Later turns need the reasoning items and tool-call IDs.
                    history.extend(response.output)

                    if not tool_calls:
                        stop_reason = "no_tool_calls"

                        (self.logs_dir / "final_answer.txt").write_text(
                            response.output_text or "",
                            encoding="utf-8",
                        )
                        break

                    boundary("tools_start", turn=turn, pending_calls=len(tool_calls))
                    for call in tool_calls:
                        boundary("tool_start", turn=turn, call_id=call.call_id, tool=call.name)
                        start_tool = time.perf_counter()

                        result = await self.execute_tool(
                            environment,
                            call.name,
                            call.arguments,
                        )

                        tool_ms = (
                            time.perf_counter() - start_tool
                        ) * 1000

                        tool_calls_total += 1
                        tool_failures += int(not result.success)
                        tool_ms_total += tool_ms

                        log({
                            "type": "tool",
                            "turn": turn,
                            "call_id": call.call_id,
                            "tool": call.name,
                            "duration_ms": round(tool_ms, 2),
                            "output_bytes": len(result.text.encode("utf-8")),
                            "success": result.success,
                            **result.retention,
                        })

                        history.append({
                            "type": "function_call_output",
                            "call_id": call.call_id,
                            "output": result.text,
                        })
                    boundary("tools_complete", turn=turn)
                else:
                    stop_reason = "max_turns"

        except asyncio.CancelledError:
            stop_reason = "cancelled_or_task_timeout"
            # Harbor uses cancellation to enforce the task deadline.
            raise

        except Exception as exc:
            stop_reason = "error"
            log({
                "type": "agent_error",
                "error_type": type(exc).__name__,
            })
            raise

        finally:
            if session_id is not None:
                async with AsyncOpenAI(max_retries=0, timeout=5) as client:
                    final_notice = await finish_session(client, self.model_name, session_id)
                log({"type": "session_final", **final_notice})
            fresh_tokens = (
                total_input - total_cached
                if total_input is not None and total_cached is not None
                else None
            )

            summary = {
                "agent_version": self.version(),
                "stop_reason": stop_reason,
                # Finishing the agent loop says nothing about the verifier reward.
                "agent_completed": stop_reason == "no_tool_calls",
                "output_limit_hit": (
                    stop_reason == "max_output_tokens"
                    if stop_reason in ("no_tool_calls", "max_turns", "max_output_tokens")
                    else None
                ),
                "model_calls": model_calls,
                "tool_calls": tool_calls_total,
                "tool_failures": tool_failures,
                "total_input_tokens": total_input,
                "total_cached_input_tokens": total_cached,
                "total_output_tokens": total_output,
                "fresh_input_tokens": fresh_tokens,
                # An aborted call can leave its cost out of these totals.
                "model_latency_ms": round(model_ms, 2),
                "tool_latency_ms": round(tool_ms_total, 2),
                "wall_time_ms": round(
                    (time.perf_counter() - started) * 1000,
                    2,
                ),
                "max_tool_output_chars": self.settings.max_tool_output,
                "tool_timeout_sec": self.settings.tool_timeout,
                "output_policy": self.settings.output_policy,
            }

            context.metadata = {
                **(context.metadata or {}),
                **summary,
            }

            log({"type": "task_summary", **summary})

    async def execute_tool(
        self,
        environment: BaseEnvironment,
        name: str,
        arguments: str | dict,
    ) -> ToolObservation:
        # Return argument errors so the model can retry.
        try:
            args = (
                json.loads(arguments)
                if isinstance(arguments, str)
                else arguments
            )

            if not isinstance(args, dict):
                raise ValueError("Tool arguments must be a JSON object")

            if name == "bash":
                command = args["command"]

                if (
                    not isinstance(command, str)
                    or not command.strip()
                    or "\0" in command
                ):
                    raise ValueError(
                        "command must be a nonempty string without NUL bytes"
                    )

            elif name == "read_file":
                path = self.safe_path(args["path"])
                command = self.file_command(path, "read")

            elif name == "write_file":
                path = self.safe_path(args["path"])
                content = args["content"]

                if not isinstance(content, str):
                    raise ValueError("content must be a string")

                encoded = base64.b64encode(
                    content.encode("utf-8")
                ).decode("ascii")

                command = self.file_command(path, "write", encoded)

            else:
                raise ValueError(f"Unknown tool: {name}")

        except (ValueError, TypeError, KeyError) as exc:
            return observation(f"ERROR: {type(exc).__name__}: {exc}", False, self.settings)

        try:
            result = await environment.exec(
                command=command,
                cwd=self.cwd,
                timeout_sec=self.settings.tool_timeout,
            )

        except (TimeoutError, asyncio.TimeoutError):
            return observation(
                f"ERROR: command timed out after {self.settings.tool_timeout} seconds",
                False, self.settings,
            )

        except RuntimeError as exc:
            # Harbor's Docker backend can wrap timeouts in RuntimeError.
            if str(exc).startswith("Command timed out after "):
                return observation(f"ERROR: {exc}", False, self.settings)

            raise

        if name == "write_file" and result.return_code == 0:
            return observation(f"Successfully wrote {path}", True, self.settings)
        return self.format_result(result)

    def format_result(self, result) -> ToolObservation:
        output = result.stdout or ""
        if result.stderr:
            output += "\nSTDERR:\n" + result.stderr
        return observation(output, result.return_code == 0, self.settings,
                           prefix=f"EXIT CODE: {result.return_code}\n")

    def safe_path(self, path: str) -> str:
        if (
            not isinstance(path, str)
            or not path.strip()
            or "\0" in path
        ):
            raise ValueError(
                "path must be a nonempty string without NUL bytes"
            )

        base = posixpath.normpath(self.cwd)
        target = posixpath.normpath(posixpath.join(base, path))

        if posixpath.commonpath([base, target]) != base:
            raise ValueError(f"Path must stay inside {base}")

        return target

    def file_command(self, path: str, operation: str, encoded: str = "") -> str:
        return shlex.join([
            "python3", "-I", "-c", FILE_TOOL_SCRIPT, self.cwd,
            posixpath.relpath(path, self.cwd), operation, encoded,
        ])
