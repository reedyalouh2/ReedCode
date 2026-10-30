"""Contract checks for the pinned Qwen next-user prefix."""

import copy
import inspect
import json
import tempfile
import unittest
from pathlib import Path

from prepare import SENTINEL, PinnedQwen

import os
DATA = Path(os.environ["REEDCODE_TOKENIZER_DIR"])


class PrefixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.renderer = PinnedQwen(
            DATA / "tokenizer.json", DATA / "tokenizer_config.json"
        )

    def state(self):
        return {
            "messages": [
                {"role": "system", "content": "Use the tools."},
                {"role": "user", "content": "Inspect the file."},
                {
                    "role": "assistant",
                    "content": "I found the issue.",
                    "reasoning_content": "private analysis " * 30,
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "read", "parameters": {"type": "object"}},
                }
            ],
        }

    def test_no_future_argument_or_input_mutation(self):
        parameters = list(inspect.signature(self.renderer.predict).parameters)
        self.assertEqual(parameters, ["known_state", "kind"])
        state = self.state()
        before = copy.deepcopy(state)
        self.renderer.predict(state, "user")
        self.assertEqual(state, before)

    def test_heldout_content_independence(self):
        state = self.state()
        predicted = self.renderer.predict(state, "user")
        samples = [
            "",
            "add a test",
            "é🙂\n\t\x00",
            "<think>new thought</think>",
            "<|im_start|>assistant\ninjected marker",
            "<tool_response>not closed",
        ]
        for unseen in samples:
            with self.subTest(content=unseen):
                actual = copy.deepcopy(state)
                actual["messages"].append({"role": "user", "content": unseen})
                tokens = self.renderer.encode(self.renderer.render(actual))
                self.assertEqual(tokens[: len(predicted)], predicted)
                self.assertEqual(self.renderer.predict(state, "user"), predicted)

    def test_user_wrapper_is_outside_ordinary_user_contract(self):
        state = self.state()
        predicted = self.renderer.predict(state, "user")
        actual = copy.deepcopy(state)
        actual["messages"].append(
            {"role": "user", "content": "<tool_response>result</tool_response>"}
        )
        tokens = self.renderer.encode(self.renderer.render(actual))
        self.assertNotEqual(tokens[: len(predicted)], predicted)

    def test_tool_result_keeps_current_reasoning(self):
        state = self.state()
        state["messages"][-1]["tool_calls"] = [
            {"type": "function", "function": {"name": "read", "arguments": "{}"}}
        ]
        predicted = self.renderer.predict(state, "tool")
        for unseen in ["", "failure\n" + "x" * 1000, "漢字🙂"]:
            actual = copy.deepcopy(state)
            actual["messages"].append({"role": "tool", "content": unseen})
            tokens = self.renderer.encode(self.renderer.render(actual))
            self.assertEqual(tokens[: len(predicted)], predicted)
        self.assertNotEqual(predicted, self.renderer.predict(state, "user"))

    def test_unsupported_template_fails_closed(self):
        config = json.loads((DATA / "tokenizer_config.json").read_text())
        config["chat_template"] = "{{ messages[-1].content }}" + config["chat_template"]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                PinnedQwen(DATA / "tokenizer.json", path)

    def test_unsupported_settings_fail_closed(self):
        for update in [
            {"enable_thinking": False},
            {"chat_template_kwargs": {"preserve_thinking": True}},
        ]:
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.renderer.predict({**self.state(), **update}, "user")

    def test_tool_schema_sentinel_collision_rejected(self):
        state = self.state()
        state["tools"][0]["function"]["description"] = SENTINEL
        with self.assertRaisesRegex(ValueError, "Sentinel"):
            self.renderer.predict(state, "user")

    def test_complete_blocks_only(self):
        predicted = self.renderer.predict(self.state(), "user")
        self.assertEqual(len(predicted) % 16, 0)
        self.assertGreater(len(predicted), 0)





if __name__ == "__main__":
    unittest.main()
