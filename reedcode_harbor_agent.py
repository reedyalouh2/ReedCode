from __future__ import annotations

import asyncio
import base64
import json
import os
import posixpath
import shlex
import time

from openai import AsyncOpenAI

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext


MAX_TURNS = int(os.getenv("MAX_TURNS", "30"))
MAX_TOOL_OUTPUT = int(os.getenv("MAX_TOOL_OUTPUT", "20000"))
TOOL_TIMEOUT = int(os.getenv("TOOL_TIMEOUT", "120"))

if min(MAX_TURNS, MAX_TOOL_OUTPUT, TOOL_TIMEOUT) <= 0:
    raise ValueError(
        "MAX_TURNS, MAX_TOOL_OUTPUT and TOOL_TIMEOUT must be positive"
    )


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
    # Missing telemetry is unknown, not zero.
    return None if total is None or value is None else total + value


def bound_output(text: str) -> str:
    # Character cap, not a token/byte cap.
    # Preserve the original A/B truncation policy.
    if len(text) > MAX_TOOL_OUTPUT:
        return text[:MAX_TOOL_OUTPUT] + "\n...[output truncated]"
    return text


class ReedCodeAgent(BaseAgent):
    @staticmethod
    def name() -> str:
        return "reedcode"

    def version(self) -> str:
        return "0.2.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        result = await environment.exec(
            command="pwd",
            timeout_sec=TOOL_TIMEOUT,
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

        history = [{"role": "user", "content": instruction}]
        trace_path = self.logs_dir / "reedcode_trace.jsonl"
        started = time.perf_counter()

        total_input = total_cached = total_output = 0
        model_calls = tool_calls_total = tool_failures = 0
        model_ms = tool_ms_total = 0.0
        stop_reason = "error"

        def log(event: dict) -> None:
            with trace_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")

        log({
            "type": "run_config",
            "agent_version": self.version(),
            "model": self.model_name,
            "max_turns": MAX_TURNS,
            "max_tool_output_chars": MAX_TOOL_OUTPUT,
            "tool_timeout_sec": TOOL_TIMEOUT,
        })

        try:
            async with AsyncOpenAI() as client:
                for turn in range(1, MAX_TURNS + 1):
                    start = time.perf_counter()

                    response = await client.responses.create(
                        model=self.model_name,
                        instructions=(
                            "You are a coding agent working inside a repository. "
                            "Inspect files, run commands, modify files when needed, "
                            "and verify your work before finishing."
                        ),
                        input=history,
                        tools=TOOLS,
                    )

                    latency_ms = (time.perf_counter() - start) * 1000
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

                    log({
                        "type": "inference",
                        "turn": turn,
                        "latency_ms": round(latency_ms, 2),
                        "input_tokens": input_tokens,
                        "cached_input_tokens": cached_tokens,
                        "output_tokens": output_tokens,
                        "tool_calls": len(tool_calls),
                    })

                    context.n_input_tokens = total_input
                    context.n_cache_tokens = total_cached
                    context.n_output_tokens = total_output

                    if response.status != "completed":
                        raise RuntimeError(
                            f"Model response status: {response.status}"
                        )

                    # Preserve reasoning items, messages and tool calls.
                    history.extend(response.output)

                    if not tool_calls:
                        stop_reason = "no_tool_calls"

                        (self.logs_dir / "final_answer.txt").write_text(
                            response.output_text or "",
                            encoding="utf-8",
                        )
                        break

                    for call in tool_calls:
                        start_tool = time.perf_counter()

                        # Decode JSON inside the guarded dispatcher.
                        result, success = await self.execute_tool(
                            environment,
                            call.name,
                            call.arguments,
                        )

                        tool_ms = (
                            time.perf_counter() - start_tool
                        ) * 1000

                        tool_calls_total += 1
                        tool_failures += int(not success)
                        tool_ms_total += tool_ms

                        log({
                            "type": "tool",
                            "turn": turn,
                            "call_id": call.call_id,
                            "tool": call.name,
                            "duration_ms": round(tool_ms, 2),
                            # Bytes returned AFTER truncation.
                            "output_bytes": len(result.encode("utf-8")),
                            "success": success,
                        })

                        history.append({
                            "type": "function_call_output",
                            "call_id": call.call_id,
                            "output": result,
                        })
                else:
                    stop_reason = "max_turns"

        except asyncio.CancelledError:
            stop_reason = "cancelled_or_task_timeout"
            # Allow Harbor to enforce its overall task deadline.
            raise

        except Exception as exc:
            stop_reason = "error"
            log({
                "type": "agent_error",
                "error_type": type(exc).__name__,
            })
            # Keep API/infrastructure/programming failures visible.
            raise

        finally:
            fresh_tokens = (
                total_input - total_cached
                if total_input is not None and total_cached is not None
                else None
            )

            summary = {
                "agent_version": self.version(),
                "stop_reason": stop_reason,
                # Completion is NOT a verifier reward.
                "agent_completed": stop_reason == "no_tool_calls",
                "model_calls": model_calls,
                "tool_calls": tool_calls_total,
                "tool_failures": tool_failures,
                "total_input_tokens": total_input,
                "total_cached_input_tokens": total_cached,
                "total_output_tokens": total_output,
                "fresh_input_tokens": fresh_tokens,
                # On aborted runs, totals cover completed calls only.
                "model_latency_ms": round(model_ms, 2),
                "tool_latency_ms": round(tool_ms_total, 2),
                "wall_time_ms": round(
                    (time.perf_counter() - started) * 1000,
                    2,
                ),
                "max_tool_output_chars": MAX_TOOL_OUTPUT,
                "tool_timeout_sec": TOOL_TIMEOUT,
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
    ) -> tuple[str, bool]:
        # Invalid model arguments become observations, not crashes.
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
                command = f"cat -- {shlex.quote(path)}"

            elif name == "write_file":
                path = self.safe_path(args["path"])
                content = args["content"]

                if not isinstance(content, str):
                    raise ValueError("content must be a string")

                encoded = base64.b64encode(
                    content.encode("utf-8")
                ).decode("ascii")

                parent = posixpath.dirname(path)

                command = (
                    f"mkdir -p -- {shlex.quote(parent)} && "
                    f"printf '%s' {shlex.quote(encoded)} | "
                    f"base64 -d > {shlex.quote(path)}"
                )

            else:
                raise ValueError(f"Unknown tool: {name}")

        except (ValueError, TypeError, KeyError) as exc:
            return (
                bound_output(f"ERROR: {type(exc).__name__}: {exc}"),
                False,
            )

        try:
            result = await environment.exec(
                command=command,
                cwd=self.cwd,
                timeout_sec=TOOL_TIMEOUT,
            )

        except (TimeoutError, asyncio.TimeoutError):
            return (
                f"ERROR: command timed out after {TOOL_TIMEOUT} seconds",
                False,
            )

        except RuntimeError as exc:
            # Harbor's Docker backend can wrap timeouts in RuntimeError.
            if str(exc).startswith("Command timed out after "):
                return bound_output(f"ERROR: {exc}"), False

            # Do not disguise unrelated infrastructure failures.
            raise

        text, success = self.format_result(result)

        if name == "write_file" and success:
            text = bound_output(f"Successfully wrote {path}")

        return text, success

    def format_result(self, result) -> tuple[str, bool]:
        output = result.stdout or ""

        if result.stderr:
            output += "\nSTDERR:\n" + result.stderr

        text = (
            f"EXIT CODE: {result.return_code}\n"
            f"{bound_output(output)}"
        )

        return text, result.return_code == 0

    def safe_path(self, path: str) -> str:
        # Lexical path guard only: not symlink-safe.
        # The bash tool is NOT confined by this check.
        # Run this agent only in isolated benchmark containers.
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
