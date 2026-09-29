import importlib.util
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1] / "experiments/dynamo-prefix"
sys.path.insert(0, str(ROOT))
import boundary_detail

TOKENIZER = Path(os.environ.get("REEDCODE_QWEN_TOKENIZER", "/tmp/reedcode-prefix-tokenizer/tokenizer.json"))
HAS_XXHASH = importlib.util.find_spec("xxhash") is not None


@unittest.skipUnless(TOKENIZER.exists() and HAS_XXHASH, "Download the pinned tokenizer and install prefix requirements")
class BoundaryDetailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.renderer = boundary_detail.audit.QwenRenderer(TOKENIZER)
        cls.report = boundary_detail.boundary_detail(TOKENIZER)

    def test_all_archived_boundaries_are_the_same_four_input_tokens(self):
        self.assertEqual(self.report["coverage"]["continuations"], 42)
        self.assertEqual(self.report["coverage"]["verified_requests"], 52)
        self.assertEqual(self.report["coverage"]["verified_input_hashes"], 9703)
        self.assertEqual(self.report["empty_thinking_suffix"]["text"], "<think>\n\n</think>\n\n")
        for row in self.report["transitions"]:
            self.assertEqual(row["classification"], "empty_thinking_suffix_removed")
            self.assertEqual(row["first_mismatch_token_index"], row["initial_prompt_tokens"] - 4)
            self.assertEqual(row["initial_suffix_token_ids"], [151667, 271, 151668, 271])

    def test_block_alignment_accounts_for_all_five_lost_blocks(self):
        lost = [row for row in self.report["transitions"] if row["previous_complete_block_tokens_lost"]]
        self.assertEqual(len(lost), 5)
        self.assertEqual(sum(row["previous_complete_block_tokens_lost"] for row in lost), 80)
        self.assertTrue(all(row["initial_input_length_mod_block_size"] in (0, 1, 2, 3) for row in lost))
        self.assertEqual(self.report["summary"]["following_starts_with_tool_call"], 15)
        self.assertEqual(self.report["summary"]["following_starts_with_assistant_prose"], 27)
        self.assertEqual(self.report["summary"]["assistants_with_nonempty_reasoning"], 0)

    def test_other_mismatch_is_not_called_a_thinking_boundary(self):
        row = boundary_detail.compare_boundary(self.renderer, [1, 2, 3], [1, 9, 10])
        self.assertEqual(row["classification"], "other")
        self.assertEqual(row["first_mismatch_token_index"], 1)

    def test_full_input_prefix_has_no_mismatch(self):
        row = boundary_detail.compare_boundary(self.renderer, [1, 2, 3], [1, 2, 3, 4])
        self.assertEqual(row["classification"], "initial_prompt_is_prefix")
        self.assertIsNone(row["first_mismatch_token_index"])


if __name__ == "__main__":
    unittest.main()
