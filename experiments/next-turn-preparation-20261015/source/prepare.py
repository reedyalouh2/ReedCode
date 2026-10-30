"""Predict only the prefix determined by a completed request and response."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

from jinja2.sandbox import ImmutableSandboxedEnvironment
from tokenizers import Tokenizer

TEMPLATE_HASH = "a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8"
TOKENIZER_HASH = "aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4"
SENTINEL = "__REEDCODE_UNOBSERVED_FUTURE_CONTENT_74932__"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def lcp(a, b):
    return next(
        (i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b))
    )


def aligned(n):
    return n // 16 * 16


class PinnedQwen:
    def __init__(self, tokenizer_path, config_path):
        raw = Path(tokenizer_path).read_bytes()
        config = json.loads(Path(config_path).read_text())
        template = config["chat_template"]
        if sha(raw) != TOKENIZER_HASH or sha(template.encode()) != TEMPLATE_HASH:
            raise ValueError(
                "Unsupported tokenizer or template; no prefix prediction is allowed"
            )
        self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
        env = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True)
        env.filters["tojson"] = lambda value, **kwargs: json.dumps(
            value, ensure_ascii=False, **kwargs
        )
        self.template = env.from_string(template)
        if self.tokenizer.encode("<|im_start|>", add_special_tokens=False).ids != [
            151644
        ]:
            raise ValueError("Boundary marker is not the pinned atomic token")

    def render(self, state):
        return self.template.render(
            messages=state["messages"],
            tools=state.get("tools"),
            add_generation_prompt=True,
            enable_thinking=True,
        )

    def encode(self, text):
        return self.tokenizer.encode(text, add_special_tokens=False).ids

    def predict_text(self, known_state, kind):
        if kind not in ("user", "tool"):
            raise ValueError("Unknown boundary type")
        if (
            set(known_state) - {"messages", "tools", "enable_thinking"}
            or known_state.get("enable_thinking", True) is not True
        ):
            raise ValueError(
                "Only pinned thinking-enabled message/tool snapshots are supported"
            )
        state = deepcopy(known_state)
        # This whitelist's template only tests the future user's wrapper status.
        # An ordinary user message makes every known assistant precede last_query_index.
        if SENTINEL in json.dumps(state):
            raise ValueError("Sentinel collision")
        state["messages"].append({"role": kind, "content": SENTINEL})
        text = self.render(state)
        location = text.index(SENTINEL)
        end = text.rfind("<|im_start|>", 0, location) + len("<|im_start|>")
        if end < len("<|im_start|>"):
            raise ValueError("No atomic boundary before unknown content")
        return text[:end]

    def predict(self, known_state, kind):
        ids = self.encode(self.predict_text(known_state, kind))
        if not ids or ids[-1] != 151644:
            raise ValueError("Boundary encoding changed")
        return ids[: aligned(len(ids))]
