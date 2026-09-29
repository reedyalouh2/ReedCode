import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1] / "experiments/dynamo-upstream/001-speculative-prefill/kv-footprint"
spec = importlib.util.spec_from_file_location("dynamo_kv_footprint", ROOT / "reproduce.py")
footprint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(footprint)


class KvFootprintTests(unittest.TestCase):
    def test_repeated_warmups_deduplicate_and_parent_mismatch_does_not_merge(self):
        identities = footprint.BlockIdentity(2)
        real = identities.blocks([1, 2, 3, 4, 5, 6])
        warm = identities.blocks([1, 2, 9, 4, 5, 6])
        self.assertEqual(len(real & warm), 1)
        self.assertEqual(len(real | warm), 5)
        self.assertEqual(warm, identities.blocks([1, 2, 9, 4, 5, 6]))

    def test_old_real_history_is_separate_from_warmup_overhead(self):
        row = footprint.snapshot({1, 2, 3}, {1, 4}, {1, 3}, 5, 20, block_size=2)
        self.assertEqual(row["older_real_blocks_outside_current_context"], 1)
        self.assertEqual(row["warmup_only_blocks"], 1)
        self.assertEqual(row["warmup_only_to_current_real_ratio"], 0.5)
        self.assertEqual(row["combined_to_current_real_ratio"], 2)
        self.assertEqual(row["current_real_partial_tokens_excluded"], 1)

    def test_future_real_request_cannot_reduce_warmup_branch_early(self):
        native = {"case": {"original": {"token_ids": [1, 2, 3, 4]},
                           "prepared": {"token_ids": [1, 2, 9, 10]},
                           "followup": {"token_ids": [1, 2, 9, 10, 11, 12]}}}
        result = footprint.session([{"case": "case"}], native, 10, block_size=2)
        self.assertEqual(result["steps"][0]["warmup_only_blocks"], 1)
        self.assertEqual(result["steps"][1]["warmup_only_blocks"], 0)
        self.assertEqual(result["terminal"]["older_real_blocks_outside_current_context"], 1)

    def test_partial_blocks_are_excluded_and_no_real_full_block_has_no_ratio(self):
        identities = footprint.BlockIdentity(4)
        self.assertEqual(identities.blocks([1, 2, 3]), set())
        row = footprint.snapshot(set(), {1}, set(), 3, 40, block_size=4)
        self.assertIsNone(row["warmup_only_to_current_real_ratio"])
        self.assertEqual(row["kv_bytes"]["warmup_only"], 40)

    def test_qwen_bf16_size_uses_kv_heads_and_both_k_and_v(self):
        config = {"num_hidden_layers": 36, "num_key_value_heads": 8, "head_dim": 128}
        self.assertEqual(footprint.kv_bytes_per_token(config), 147456)
        self.assertEqual(footprint.kv_bytes_per_token(config) * 16, 2359296)

    def test_pinned_inputs_are_verified_before_accounting(self):
        report, native, config = footprint.load_evidence()
        self.assertEqual(len(native), 54)
        self.assertEqual(len(report["transitions"]), 42)
        self.assertEqual(config["torch_dtype"], "bfloat16")

    def test_all_saved_snapshots_match_an_independent_prefix_union_count(self):
        _, native, _ = footprint.load_evidence()
        result = json.loads((ROOT / "results.json").read_text())
        self.assertEqual(result["script_sha256"], footprint.sha((ROOT / "reproduce.py").read_bytes()))

        def common(a, b):
            for index, (left, right) in enumerate(zip(a, b)):
                if left != right:
                    return index
            return min(len(a), len(b))

        def union_count(paths):
            previous, total = [], 0
            for path in paths:
                shared = max((common(path, old) // 16 for old in previous), default=0)
                total += len(path) // 16 - shared
                previous.append(path)
            return total

        checked = 0
        for session in result["recorded_sessions"] + result["constructed_growth"]:
            real, warm = [], []
            for step in session["steps"]:
                case = native[step["case"]]
                if step["stage"] == "after_warmup":
                    real.append(case["original"]["token_ids"])
                    warm.append(case["prepared"]["token_ids"])
                else:
                    real.append(case["followup"]["token_ids"])
                self.assertEqual(union_count(real), step["real_history_blocks"])
                self.assertEqual(union_count(warm), step["warmup_history_blocks"])
                self.assertEqual(union_count(real + warm), step["combined_blocks"])
                self.assertEqual(union_count(real + warm) - union_count(real), step["warmup_only_blocks"])
                checked += 1
        self.assertEqual(checked, 108)


if __name__ == "__main__":
    unittest.main()
