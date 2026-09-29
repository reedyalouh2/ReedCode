import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1] / "experiments/dynamo-prefix"
spec = importlib.util.spec_from_file_location("user_boundary_audit", ROOT / "audit.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
spec = importlib.util.spec_from_file_location("user_boundary_probe", ROOT / "user_boundary.py")
boundary = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"audit": audit}):
    spec.loader.exec_module(boundary)
TOKENIZER = Path(os.environ.get("REEDCODE_QWEN_TOKENIZER", "/tmp/reedcode-prefix-tokenizer/tokenizer.json"))
HAS_RENDER_DEPS = all(importlib.util.find_spec(name) is not None for name in ("jinja2", "tokenizers"))


class UserBoundaryFixtureTests(unittest.TestCase):
    def test_three_boundaries_append_completed_assistant_and_new_user(self):
        pairs = boundary.fixture(1, 2)
        self.assertEqual([pair["user_boundary"] for pair in pairs], [1, 2, 3])
        for index, pair in enumerate(pairs, 1):
            before = pair["request"]["messages"]
            after = pair["following"]["messages"]
            self.assertEqual(after[:-2], before)
            self.assertEqual(after[-2], pair["completed_assistant"])
            self.assertEqual(after[-1]["role"], "user")
            self.assertEqual(sum(m["role"] == "user" for m in before), index)
            audit.check_followup_contract(pair["request"], pair["completed_assistant"], pair["following"])

    def test_invalid_fixture_sizes_cannot_silently_create_empty_probe(self):
        for words, lines in ((-1, 16), (True, 16), (1, 0), (1, -1), (1, True), (1.5, 16)):
            with self.subTest(words=words, lines=lines), self.assertRaises(ValueError):
                boundary.fixture(words, lines)

    def test_history_compaction_is_rejected_before_rendering(self):
        pair = boundary.fixture(1, 2)[0]
        pair["following"]["messages"][0]["content"] = "A compacted summary replaces the history."
        with self.assertRaisesRegex(ValueError, "history"):
            boundary.measure(None, pair)

    def test_changed_tools_cannot_be_mislabeled_template_rewrite(self):
        pair = boundary.fixture(1, 2)[0]
        pair["following"]["tools"][0]["function"]["description"] = "Changed tool contract."
        with self.assertRaisesRegex(ValueError, "tools"):
            boundary.measure(None, pair)


@unittest.skipUnless(TOKENIZER.exists() and HAS_RENDER_DEPS,
                     "Download the pinned tokenizer and install prefix requirements")
class QwenUserBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.renderer = audit.QwenRenderer(TOKENIZER)

    def ids(self, request, generation=True):
        return self.renderer.tokens(self.renderer.render(request, request["messages"], generation=generation))

    def test_no_reasoning_control_keeps_historical_body_unchanged(self):
        for pair in boundary.fixture(0, 16):
            with self.subTest(boundary=pair["user_boundary"]):
                result = boundary.measure(self.renderer, pair)
                self.assertEqual(result["cause"], "append_only")
                body = self.ids(pair["request"], generation=False)
                following = self.ids(pair["following"])
                self.assertEqual(body, following[:len(body)])
                self.assertFalse(result["following_reasoning_marker_present"])

    def test_one_word_can_break_reuse_before_a_long_retained_tool_result(self):
        pair = boundary.fixture(1, 512)[0]
        result = boundary.measure(self.renderer, pair)
        self.assertEqual(result["cause"], "template_history_rewrite")
        self.assertTrue(result["old_reasoning_marker_present"])
        self.assertFalse(result["following_reasoning_marker_present"])
        self.assertGreater(result["following_tokens_past_shared_blocks"], 6000)
        self.assertIn("diagnostic line 511", pair["following"]["messages"][-3]["content"])
        self.assertLess(result["common_prefix_tokens"], len(self.ids(pair["request"], generation=False)))

    def test_removed_reasoning_length_does_not_change_retained_next_request(self):
        short_pairs = boundary.fixture(1, 512)
        long_pairs = boundary.fixture(2048, 512)
        for short, long in zip(short_pairs, long_pairs):
            with self.subTest(boundary=short["user_boundary"]):
                short_old, long_old = self.ids(short["request"]), self.ids(long["request"])
                self.assertGreater(len(long_old), len(short_old))
                self.assertEqual(self.ids(short["following"]), self.ids(long["following"]))
                a, b = boundary.measure(self.renderer, short), boundary.measure(self.renderer, long)
                self.assertEqual(a["following_tokens_past_shared_blocks"], b["following_tokens_past_shared_blocks"])
                self.assertGreater(b["prior_complete_block_tokens_past_divergence"],
                                   a["prior_complete_block_tokens_past_divergence"])

    def test_all_three_boundaries_measure_the_current_turn_rewrite(self):
        rows = [boundary.measure(self.renderer, pair) for pair in boundary.fixture(1, 16)]
        self.assertTrue(all(row["cause"] == "template_history_rewrite" for row in rows))
        self.assertTrue(all(row["old_reasoning_marker_present"] for row in rows))
        self.assertTrue(all(not row["following_reasoning_marker_present"] for row in rows))
        self.assertEqual(sorted(row["common_prefix_tokens"] for row in rows),
                         [row["common_prefix_tokens"] for row in rows])
        self.assertEqual(len({row["common_prefix_tokens"] for row in rows}), 3)

    def test_probe_keeps_authored_fixtures_separate_from_gpu_evidence(self):
        report = boundary.run(TOKENIZER)
        self.assertEqual(report["real_agent_sessions"], 0)
        self.assertEqual(report["model_calls"], 0)
        self.assertIs(report["gpu_measurements"], False)
        self.assertEqual(len(report["cases"]), 6)
        self.assertEqual(sum(len(case["boundaries"]) for case in report["cases"]), 18)


if __name__ == "__main__":
    unittest.main()
