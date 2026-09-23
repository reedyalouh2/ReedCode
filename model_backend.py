from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace


@dataclass
class ChatMessage:
    message: dict
    type: str = field(default="chat_message", init=False)


@dataclass
class ChatFunctionCall:
    name: str
    arguments: str
    call_id: str
    type: str = field(default="function_call", init=False)


def response_termination(response) -> dict:
    details = getattr(response, "incomplete_details", None)
    reason = details.get("reason") if isinstance(details, dict) else getattr(details, "reason", None)
    finish = getattr(response, "finish_reason", None)
    status = response.status
    return {
        "response_status": status,
        "finish_reason": finish,
        "incomplete_reason": reason,
        "output_limit_hit": status == "incomplete" and (finish == "length" or reason == "max_output_tokens"),
    }


def chat_messages(instructions: str, history: list) -> list[dict]:
    messages = [{"role": "system", "content": instructions}]
    for item in history:
        if isinstance(item, ChatMessage):
            # vLLM versions use different names for reasoning.
            messages.append({
                key: value for key, value in item.message.items()
                if key in {"role", "content", "tool_calls", "reasoning", "reasoning_content"}
            })
        elif isinstance(item, ChatFunctionCall):
            # The full assistant message already contains these calls.
            continue
        elif isinstance(item, dict) and item.get("type") == "function_call_output":
            messages.append({
                "role": "tool",
                "tool_call_id": item["call_id"],
                "content": item["output"],
            })
        elif isinstance(item, dict) and item.get("role") in {"user", "system"}:
            messages.append(dict(item))
        else:
            raise ValueError("Chat history contains an unsupported item; do not switch APIs mid-run")
    return messages


async def create_response(
    client, *, api: str, model: str, instructions: str,
    input: list, tools: list[dict], max_output_tokens: int | None = None,
):
    if max_output_tokens is not None and max_output_tokens < 1:
        raise ValueError("max_output_tokens must be positive")

    if api == "responses":
        options = {} if max_output_tokens is None else {"max_output_tokens": max_output_tokens}
        return await client.responses.create(
            model=model, instructions=instructions, input=input, tools=tools, **options,
        )
    if api != "chat":
        raise ValueError(f"Unsupported model API: {api}")

    chat_tools = []
    for tool in tools:
        if tool.get("type") != "function":
            raise ValueError("The Chat backend supports function tools only")
        chat_tools.append({
            "type": "function",
            "function": {key: value for key, value in tool.items() if key != "type"},
        })
    options = {} if max_output_tokens is None else {"max_completion_tokens": max_output_tokens}
    completion = await client.chat.completions.create(
        model=model, messages=chat_messages(instructions, input),
        tools=chat_tools, tool_choice="auto", **options,
    )
    if len(completion.choices) != 1:
        raise RuntimeError("Expected exactly one Chat completion choice")

    choice = completion.choices[0]
    message = choice.message
    status = "completed" if choice.finish_reason in {"stop", "tool_calls"} else "incomplete"
    output = [ChatMessage(message.model_dump(exclude_none=True))]
    # A cut-off response may have missing IDs or unfinished arguments.
    for call in (message.tool_calls or []) if status == "completed" else []:
        if call.type != "function" or not call.id:
            raise RuntimeError("Chat completion contains an unsupported tool call")
        output.append(ChatFunctionCall(
            name=call.function.name, arguments=call.function.arguments, call_id=call.id,
        ))

    usage = completion.usage
    details = getattr(usage, "prompt_tokens_details", None)
    normalized_usage = SimpleNamespace(
        input_tokens=getattr(usage, "prompt_tokens", None),
        output_tokens=getattr(usage, "completion_tokens", None),
        input_tokens_details=SimpleNamespace(cached_tokens=getattr(details, "cached_tokens", None)),
    )
    return SimpleNamespace(
        output=output, output_text=message.content or "", usage=normalized_usage,
        status=status, finish_reason=choice.finish_reason, model=completion.model,
    )
