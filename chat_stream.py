"""Assemble Chat Completions streams and time the first output delta."""

import time

from openai.types.chat import ChatCompletion


async def stream_completion(client, **request):
    started = time.perf_counter()
    timing = {"first_output_ms": None, "stream_ms": None, "chunks": 0}
    stream = None
    message = {"role": "assistant", "content": None}
    calls = {}
    finish = usage = model = completion_id = None
    try:
        stream = await client.chat.completions.create(
            **request, stream=True, stream_options={"include_usage": True})
        async for chunk in stream:
            timing["chunks"] += 1
            if completion_id is not None and chunk.id != completion_id:
                raise RuntimeError("Completion ID changed during the stream")
            completion_id, model = chunk.id, chunk.model
            if chunk.usage is not None:
                usage = chunk.usage.model_dump()
            for choice in chunk.choices:
                if choice.index != 0 or len(chunk.choices) != 1:
                    raise RuntimeError("Expected one streamed completion")
                delta = choice.delta.model_dump(exclude_none=True)
                if finish is not None and any(delta.get(k) for k in (
                    "content", "reasoning", "reasoning_content", "tool_calls")):
                    raise RuntimeError("Output received after the finish reason")
                visible = False
                for key in ("content", "reasoning", "reasoning_content", "refusal"):
                    value = delta.get(key)
                    if value:
                        if not isinstance(value, str):
                            raise RuntimeError("Unsupported streamed content")
                        message[key] = (message.get(key) or "") + value
                        visible = True
                if delta.get("function_call"):
                    raise RuntimeError("Legacy streamed function calls are unsupported")
                for part in delta.get("tool_calls", []):
                    index = part["index"]
                    call = calls.setdefault(index, {"id": None, "type": "function",
                                                    "function": {"name": "", "arguments": ""}})
                    if part.get("type", "function") != "function":
                        raise RuntimeError("Unsupported streamed tool")
                    if part.get("id"):
                        if call["id"] not in (None, part["id"]):
                            raise RuntimeError("Tool call ID changed during the stream")
                        call["id"] = part["id"]
                    for key in ("name", "arguments"):
                        value = (part.get("function") or {}).get(key)
                        if value:
                            call["function"][key] += value
                            visible = True
                if visible and timing["first_output_ms"] is None:
                    timing["first_output_ms"] = (time.perf_counter() - started) * 1000
                if choice.finish_reason is not None:
                    if finish is not None:
                        raise RuntimeError("Duplicate finish reason")
                    finish = choice.finish_reason
        if finish is None:
            raise RuntimeError("Stream ended without a finish reason")
        if calls:
            if sorted(calls) != list(range(len(calls))):
                raise RuntimeError("Missing tool call index")
            message["tool_calls"] = [calls[index] for index in sorted(calls)]
        # Length-limited tool arguments are kept as data but never executed.
        if finish == "length":
            for call in message.get("tool_calls", []):
                call["id"] = call["id"] or ""
        completion = ChatCompletion.model_validate({
            "id": completion_id, "object": "chat.completion", "created": 0, "model": model,
            "choices": [{"index": 0, "finish_reason": finish, "message": message}], "usage": usage})
        return completion, timing
    except BaseException as exc:
        exc.stream_timing = timing
        raise
    finally:
        timing["stream_ms"] = (time.perf_counter() - started) * 1000
        if stream is not None:
            await stream.close()
