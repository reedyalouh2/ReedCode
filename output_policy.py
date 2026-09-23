"""Retention policies for text returned by tools."""

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    max_turns: int = 30
    max_tool_output: int = 20000
    tool_timeout: int = 120
    output_policy: str = "head"

    def __post_init__(self):
        if min(self.max_turns, self.max_tool_output, self.tool_timeout) <= 0:
            raise ValueError("Turn, output and timeout limits must be positive")
        if self.output_policy not in ("head", "head_tail"):
            raise ValueError("OUTPUT_POLICY must be head or head_tail")

    @classmethod
    def from_env(cls):
        return cls(
            max_turns=int(os.getenv("MAX_TURNS", "30")),
            max_tool_output=int(os.getenv("MAX_TOOL_OUTPUT", "20000")),
            tool_timeout=int(os.getenv("TOOL_TIMEOUT", "120")),
            output_policy=os.getenv("OUTPUT_POLICY", "head"),
        )


def retain_output(text: str, limit: int, policy: str) -> tuple[str, dict]:
    if limit <= 0 or policy not in ("head", "head_tail"):
        raise ValueError("Expected a positive limit and head or head_tail policy")
    truncated = len(text) > limit
    head = text
    tail = ""
    if truncated:
        head_count = limit if policy == "head" else (limit + 1) // 2
        tail_count = limit - head_count
        head = text[:head_count]
        tail = text[-tail_count:] if tail_count else ""
    retained = head + tail
    visible = text
    if truncated:
        visible = head + "\n...[output truncated]"
        if tail:
            visible += "\n" + tail
    return visible, {
        "output_policy": policy,
        "output_limit_chars": limit,
        "truncated": truncated,
        "original_output_chars": len(text),
        "original_output_bytes": len(text.encode("utf-8")),
        "retained_output_chars": len(retained),
        "retained_output_bytes": len(retained.encode("utf-8")),
    }


@dataclass(frozen=True)
class ToolObservation:
    text: str
    success: bool
    retention: dict


def observation(text: str, success: bool, settings: Settings, prefix: str = "") -> ToolObservation:
    visible, metadata = retain_output(text, settings.max_tool_output, settings.output_policy)
    return ToolObservation(prefix + visible, success, metadata)
