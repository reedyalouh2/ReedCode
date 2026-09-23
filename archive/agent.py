import time
import json
import subprocess
from pathlib import Path

from openai import OpenAI

client = OpenAI()

WORKSPACE = Path("./workspace").resolve()
TRACE_FILE = Path("./trace.jsonl")

MAX_TURNS = 30
BASH_TIMEOUT = 30
MAX_TOOL_OUTPUT = 20_000


TOOLS = [
    {
        "type": "function",
        "name": "read_file",
        "description": "Read a text file from the coding workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to the workspace",
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "write_file",
        "description": "Write text to a file inside the coding workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to the workspace",
                },
                "content": {
                    "type": "string",
                    "description": "Complete contents to write to the file",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "bash",
        "description": "Run a shell command inside the coding workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute",
                }
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


def log_trace(event: dict):
    with TRACE_FILE.open("a") as f:
        f.write(json.dumps(event) + "\n")


def resolve_workspace_path(path: str) -> Path:
    target = (WORKSPACE / path).resolve()

    if WORKSPACE not in target.parents and target != WORKSPACE:
        raise ValueError("path outside workspace")

    return target


def read_file(path: str):
    try:
        target = resolve_workspace_path(path)
    except ValueError:
        return "ERROR: path outside workspace", False

    if not target.exists():
        return f"ERROR: {path} does not exist", False

    if not target.is_file():
        return f"ERROR: {path} is not a file", False

    return target.read_text(), True


def write_file(path: str, content: str):
    try:
        target = resolve_workspace_path(path)
    except ValueError:
        return "ERROR: path outside workspace", False

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)

    return f"Successfully wrote {path}", True


def bash(command: str):
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=WORKSPACE,
            capture_output=True,
            text=True,
            timeout=BASH_TIMEOUT,
        )

    except subprocess.TimeoutExpired:
        return (
            f"ERROR: command timed out after {BASH_TIMEOUT} seconds",
            False,
        )

    output = result.stdout

    if result.stderr:
        output += "\nSTDERR:\n" + result.stderr

    if len(output) > MAX_TOOL_OUTPUT:
        output = output[:MAX_TOOL_OUTPUT]
        output += "\n...[output truncated]"

    text = (
        f"EXIT CODE: {result.returncode}\n"
        f"{output}"
    )

    return text, result.returncode == 0


def execute_tool(name: str, arguments: dict):
    if name == "read_file":
        return read_file(arguments["path"])

    if name == "write_file":
        return write_file(
            arguments["path"],
            arguments["content"],
        )

    if name == "bash":
        return bash(arguments["command"])

    return f"ERROR: unknown tool {name}", False


def run_agent(task: str):
    history = [
        {
            "role": "user",
            "content": task,
        }
    ]

    task_start = time.perf_counter()

    model_calls = 0
    tool_call_count = 0

    total_model_latency_ms = 0
    total_tool_latency_ms = 0

    total_input_tokens = 0
    total_output_tokens = 0

    for turn in range(MAX_TURNS):
        print(f"\n--- TURN {turn + 1} ---")

        inference_start = time.perf_counter()

        response = client.responses.create(
            model="gpt-5.6-terra",
            instructions=(
                "You are a coding agent working inside a repository. "
                "Use tools when you need information from the workspace. "
                "You may read and modify files and run shell commands "
                "to complete the user's task. "
                "Verify your work before finishing."
            ),
            input=history,
            tools=TOOLS,
        )

        latency_ms = (
            time.perf_counter() - inference_start
        ) * 1000

        model_calls += 1
        total_model_latency_ms += latency_ms

        tool_calls = [
            item
            for item in response.output
            if item.type == "function_call"
        ]

        usage = response.usage

        input_tokens = getattr(
            usage,
            "input_tokens",
            0,
        )

        output_tokens = getattr(
            usage,
            "output_tokens",
            0,
        )

        input_details = getattr(
            usage,
            "input_tokens_details",
            None,
        )

        cached_input_tokens = (
            getattr(input_details, "cached_tokens", 0)
            if input_details
            else 0
        )

        total_input_tokens += input_tokens
        total_output_tokens += output_tokens

        inference_trace = {
            "type": "inference",
            "turn": turn + 1,
            "latency_ms": round(latency_ms, 2),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_input_tokens": cached_input_tokens,
            "tool_calls": len(tool_calls),
        }

        log_trace(inference_trace)

        print("\nINFERENCE METRICS:")
        print(json.dumps(inference_trace, indent=2))

        # Preserve model output in context.
        history += response.output

        if not tool_calls:
            wall_time_ms = (
                time.perf_counter() - task_start
            ) * 1000

            summary = {
                "type": "task_summary",
                "agent_completed": True,
                "model_calls": model_calls,
                "tool_calls": tool_call_count,
                "total_input_tokens": total_input_tokens,
                "total_output_tokens": total_output_tokens,
                "model_latency_ms": round(
                    total_model_latency_ms,
                    2,
                ),
                "tool_latency_ms": round(
                    total_tool_latency_ms,
                    2,
                ),
                "wall_time_ms": round(
                    wall_time_ms,
                    2,
                ),
            }

            log_trace(summary)

            print("\nAGENT RESPONSE:")
            print(response.output_text)

            print("\nTASK SUMMARY:")
            print(json.dumps(summary, indent=2))

            return

        for call in tool_calls:
            arguments = json.loads(call.arguments)

            print(
                f"\nTOOL CALL: "
                f"{call.name}({arguments})"
            )

            tool_start = time.perf_counter()

            result, success = execute_tool(
                call.name,
                arguments,
            )

            tool_latency_ms = (
                time.perf_counter() - tool_start
            ) * 1000

            tool_call_count += 1
            total_tool_latency_ms += tool_latency_ms

            output_bytes = len(
                result.encode("utf-8")
            )

            tool_trace = {
                "type": "tool",
                "turn": turn + 1,
                "tool": call.name,
                "duration_ms": round(
                    tool_latency_ms,
                    2,
                ),
                "output_bytes": output_bytes,
                "success": success,
            }

            log_trace(tool_trace)

            print(f"TOOL RESULT:\n{result}")

            print("\nTOOL METRICS:")
            print(json.dumps(tool_trace, indent=2))

            history.append(
                {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": result,
                }
            )

    # Hit MAX_TURNS without natural completion.
    wall_time_ms = (
        time.perf_counter() - task_start
    ) * 1000

    summary = {
        "type": "task_summary",
        "agent_completed": False,
        "model_calls": model_calls,
        "tool_calls": tool_call_count,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "model_latency_ms": round(
            total_model_latency_ms,
            2,
        ),
        "tool_latency_ms": round(
            total_tool_latency_ms,
            2,
        ),
        "wall_time_ms": round(
            wall_time_ms,
            2,
        ),
    }

    log_trace(summary)

    print("\nTASK SUMMARY:")
    print(json.dumps(summary, indent=2))

    print(
        f"\nAgent stopped: maximum number "
        f"of turns ({MAX_TURNS}) reached."
    )


if __name__ == "__main__":
    WORKSPACE.mkdir(
        parents=True,
        exist_ok=True,
    )

    task = input("Task: ")
    run_agent(task)