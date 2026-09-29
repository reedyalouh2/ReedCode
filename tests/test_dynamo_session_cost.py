import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1] / "experiments/dynamo-upstream/001-speculative-prefill/session-cost"
spec = importlib.util.spec_from_file_location("dynamo_session_cost", ROOT / "reproduce.py")
cost = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cost)


class PrefixCostTests(unittest.TestCase):
    def test_incompatible_with_followup_can_still_hit_prior_warmup(self):
        warmup = list(range(40))
        following = warmup[:3] + [100] * 37
        row = cost.cost(warmup, following, [[500] * 40], [warmup[:32]])
        self.assertEqual(row["incompatible_suffix_tokens"], 37)
        self.assertEqual(row["nonmatching_full_blocks"], 2)
        self.assertEqual(row["known_normal_and_warmup_prefix_tokens"], 32)
        self.assertEqual(row["uncovered_input_tokens_with_prior_warmups"], 8)
        self.assertEqual(row["uncovered_full_block_tokens_with_prior_warmups"], 0)

    def test_shared_late_content_does_not_restore_prefix_after_divergence(self):
        target = list(range(48))
        previous = [-1] + target[1:]
        self.assertEqual(cost.prefix_coverage(target, [previous]), 0)

    def test_mixed_block_and_trailing_partial_block_stay_separate(self):
        warmup = list(range(35))
        following = warmup[:17] + [100] * 18
        row = cost.cost(warmup, following, [], [])
        self.assertEqual(row["incompatible_suffix_tokens"], 18)
        self.assertEqual(row["nonmatching_full_block_tokens"], 16)
        self.assertEqual(row["trailing_partial_tokens"], 3)

    def test_full_prompt_hit_keeps_a_block_for_last_input_token(self):
        target = list(range(32))
        self.assertEqual(cost.prefix_coverage(target, [target]), 16)


if __name__ == "__main__":
    unittest.main()
