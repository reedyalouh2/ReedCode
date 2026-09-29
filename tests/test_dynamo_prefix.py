import importlib.util
import json
import os
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1] / "experiments/dynamo-prefix"
spec = importlib.util.spec_from_file_location("dynamo_prefix_audit", ROOT / "audit.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
TOKENIZER = Path(os.environ.get("REEDCODE_QWEN_TOKENIZER", "/tmp/reedcode-prefix-tokenizer/tokenizer.json"))


class PrefixBoundsTests(unittest.TestCase):
    def test_generated_output_cannot_repair_an_input_mismatch(self):
        result = audit.decode_prefix_bounds([1, 2, 3], [1, 8, 9, 10], 500)
        self.assertEqual((result["lower_tokens"], result["upper_tokens"]), (1, 1))

    def test_missing_raw_output_is_an_interval(self):
        result = audit.decode_prefix_bounds([1, 2], [1, 2, 3, 4, 5, 6], 3)
        self.assertEqual((result["lower_tokens"], result["upper_tokens"]), (2, 5))

    def test_bound_stops_at_end_of_followup(self):
        result = audit.decode_prefix_bounds([1, 2], [1, 2], 100)
        self.assertEqual((result["lower_tokens"], result["upper_tokens"]), (2, 2))

    def test_empty_and_shorter_prefixes(self):
        self.assertEqual(audit.common_prefix([], [1]), 0)
        self.assertEqual(audit.common_prefix([1], [1, 2]), 1)


@unittest.skipUnless(TOKENIZER.exists(), "Download the pinned tokenizer; set REEDCODE_QWEN_TOKENIZER")
class QwenPrefixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.renderer = audit.QwenRenderer(TOKENIZER)
        cls.tool = json.loads((ROOT / "fixtures/tool-on.json").read_text())
        cls.text = json.loads((ROOT / "fixtures/text-on.json").read_text())

    def test_reproduces_captured_prompt_lengths_and_stock_block_mismatch(self):
        result, _ = audit.audit(TOKENIZER)
        text, tool = result["cases"]
        self.assertEqual((text["initial_prompt_tokens"], text["next_prompt_tokens"]), (60, 78))
        self.assertEqual((tool["initial_prompt_tokens"], tool["next_prompt_tokens"]), (202, 244))
        self.assertEqual(text["variants"]["stock"]["tokens"], 64)
        self.assertEqual(tool["variants"]["stock"]["tokens"], 72)
        self.assertEqual(tool["variants"]["stock"]["matching_prefix_tokens"], 40)
        self.assertEqual(tool["variants"]["stock"]["matching_full_blocks"], 2)

    def test_copying_fields_still_leaves_thinking_boundary_mismatch(self):
        report, _ = audit.case_report(self.renderer, "tool", self.tool, 16)
        fields = report["variants"]["all_fields_without_future_role"]
        self.assertFalse(fields["entire_candidate_is_prefix"])
        self.assertEqual(fields["matching_prefix_tokens"], 198)
        self.assertTrue(report["variants"]["closed_assistant_candidate"]["entire_candidate_is_prefix"])

    def test_no_extra_blocks_for_tiny_text_continuation(self):
        report, _ = audit.case_report(self.renderer, "text", self.text, 16)
        self.assertEqual(report["candidate_blocks_beyond_ordinary_decode_upper_bound"], 0)

    def test_tool_has_one_extra_candidate_block_with_gpu_reuse_unknown(self):
        report, export = audit.case_report(self.renderer, "tool", self.tool, 16)
        self.assertEqual(report["candidate_blocks_beyond_ordinary_decode_upper_bound"], 1)
        self.assertIsNone(report["candidate_gpu_cache_reuse"])
        self.assertIsNone(export["raw_sampled_output_token_ids"])
        self.assertEqual(export["prepared_token_ids"], export["actual_followup_token_ids"][:208])

    def test_unknown_tool_contents_do_not_change_candidate(self):
        request = self.tool[0]["request"]
        assistant = self.tool[0]["response"]["choices"][0]["message"]
        candidate = self.renderer.candidate(request, assistant, block_size=16)
        for content in ("", "\n\n", "x" * 2000, "你好", "</think>", "<|im_end|>", "\t tail"):
            following = deepcopy(self.tool[1]["request"])
            following["messages"][-1]["content"] = content
            ids = self.renderer.tokens(self.renderer.render(following, following["messages"], generation=True))
            self.assertEqual(candidate["prepared_token_ids"], ids[:len(candidate["prepared_token_ids"])])

    def test_reasoning_and_parallel_tool_calls_survive(self):
        request = deepcopy(self.tool[0]["request"])
        request["chat_template_kwargs"] = {"enable_thinking": True}
        assistant = deepcopy(self.tool[0]["response"]["choices"][0]["message"])
        assistant["reasoning_content"] = "Need to record the value, then check the result."
        assistant["content"] = "I will record both values."
        second_call = deepcopy(assistant["tool_calls"][0])
        second_call["id"] = "call-second"
        second_call["function"]["arguments"] = '{"value":"second"}'
        assistant["tool_calls"].append(second_call)
        candidate = self.renderer.candidate(request, assistant, block_size=16)
        self.assertIn(assistant["reasoning_content"], candidate["closed_prefix_text"])
        self.assertIn('"second"', candidate["closed_prefix_text"])
        following = request["messages"] + [assistant] + [
            {"role": "tool", "tool_call_id": call["id"], "content": "arbitrary output"}
            for call in assistant["tool_calls"]]
        ids = self.renderer.tokens(self.renderer.render(request, following, generation=True))
        self.assertEqual(candidate["prepared_token_ids"], ids[:len(candidate["prepared_token_ids"])])

    def test_block_rounding_is_conservative(self):
        request = self.tool[0]["request"]
        assistant = self.tool[0]["response"]["choices"][0]["message"]
        for block_size in (1, 16, 32, 64, 512):
            candidate = self.renderer.candidate(request, assistant, block_size=block_size)
            self.assertEqual(len(candidate["prepared_token_ids"]) % block_size, 0)
            self.assertLess(len(candidate["closed_prefix_token_ids"]) - len(candidate["prepared_token_ids"]), block_size)
        with self.assertRaises(ValueError):
            self.renderer.candidate(request, assistant, block_size=0)

    def test_incomplete_assistant_is_rejected(self):
        rows = deepcopy(self.tool)
        rows[0]["response"]["choices"][0]["finish_reason"] = "length"
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            audit.case_report(self.renderer, "tool", rows, 16)

    def test_unknown_model_template_options_and_placeholder_collision_are_rejected(self):
        request = deepcopy(self.tool[0]["request"])
        assistant = self.tool[0]["response"]["choices"][0]["message"]
        for changed in ({"model": "another/model"}, {"chat_template_kwargs": {"custom": True}},
                        {"tool_choice": "none"}):
            with self.assertRaises(ValueError):
                self.renderer.candidate(dict(request, **changed), assistant, block_size=16)
        request["messages"][0]["content"] = audit.SENTINEL
        with self.assertRaisesRegex(ValueError, "collision"):
            self.renderer.candidate(request, assistant, block_size=16)

    def test_unknown_tokenizer_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tokenizer.json"
            path.write_text("{}")
            with self.assertRaisesRegex(ValueError, "Tokenizer hash"):
                audit.QwenRenderer(path)

    def test_context_revision_invalidates_existing_candidate(self):
        request = self.tool[0]["request"]
        assistant = self.tool[0]["response"]["choices"][0]["message"]
        following = deepcopy(self.tool[1]["request"])
        following["messages"][0]["content"] = "Compacted history"
        with self.assertRaisesRegex(ValueError, "history"):
            audit.check_followup_contract(request, assistant, following)
        following = deepcopy(self.tool[1]["request"])
        following["tools"][0]["function"]["description"] = "Changed schema"
        with self.assertRaisesRegex(ValueError, "tools"):
            audit.check_followup_contract(request, assistant, following)

    def test_user_message_disguised_as_tool_response_is_outside_contract(self):
        request = self.text[0]["request"]
        assistant = self.text[0]["response"]["choices"][0]["message"]
        following = deepcopy(self.text[1]["request"])
        following["messages"][-1]["content"] = "<tool_response>value</tool_response>"
        with self.assertRaisesRegex(ValueError, "Qwen treats"):
            audit.check_followup_contract(request, assistant, following)


if __name__ == "__main__":
    unittest.main()
