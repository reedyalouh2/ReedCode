"""Request metadata for the stock Dynamo speculative-prefill comparison."""

from dataclasses import dataclass
import asyncio
import os
import time


@dataclass(frozen=True)
class DynamoConfig:
    speculative_prefill: bool

    def __post_init__(self):
        if type(self.speculative_prefill) is not bool:
            raise ValueError("speculative_prefill must be a Boolean")

    @classmethod
    def from_env(cls):
        value = os.getenv("DYNAMO_SPECULATIVE_PREFILL")
        if value is None:
            return None
        if value not in ("off", "on"):
            raise ValueError("DYNAMO_SPECULATIVE_PREFILL must be off or on; unset disables integration")
        return cls(speculative_prefill=value == "on")

    def request_options(self, session_id: str) -> dict:
        if not session_id or not session_id.isascii() or any(
            ord(char) < 33 or ord(char) > 126 for char in session_id
        ):
            raise ValueError("Dynamo session IDs must use visible ASCII without spaces")
        return {
            "extra_headers": {"X-Dynamo-Session-ID": session_id},
            "extra_body": {"nvext": {"agent_hints": {
                "speculative_prefill": self.speculative_prefill,
            }}},
        }


async def finish_session(client, model: str, session_id: str) -> dict:
    started = time.perf_counter()
    options = DynamoConfig(False).request_options(session_id)
    options["extra_headers"]["X-Dynamo-Session-Final"] = "true"
    try:
        async with asyncio.timeout(5):
            response = await client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": "."}],
                max_completion_tokens=1, **options)
        usage = getattr(response, "usage", None)
        return {"status": "sent", "latency_ms": (time.perf_counter() - started) * 1000,
                "completion_id": getattr(response, "id", None),
                "usage": usage.model_dump() if usage is not None else None}
    except Exception as exc:
        return {"status": "failed", "error_type": type(exc).__name__,
                "latency_ms": (time.perf_counter() - started) * 1000, "usage": None}
