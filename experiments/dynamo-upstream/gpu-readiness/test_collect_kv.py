import unittest


def batch(events):
    return [123.0, events, 0]

from collect_kv import PublishedBlocks


def stored(hashes=(1, 2), parent=None):
    return {"type": "BlockStored", "block_hashes": list(hashes), "parent_block_hash": parent,
            "block_size": 16, "token_ids": list(range(16 * len(hashes))),
            "medium": "GPU", "lora_id": None, "lora_name": None}


class KVTests(unittest.TestCase):
    def test_start_is_unknown_until_clear(self):
        ledger = PublishedBlocks()
        self.assertFalse(ledger.accept(5, "a", batch([stored()]))["complete_since_clear"])
        result = ledger.accept(6, "b", batch([{"type": "AllBlocksCleared"}]))
        self.assertTrue(result["complete_since_clear"])
        self.assertEqual(result["published_gpu_blocks"], 0)

    def test_duplicate_and_republication_do_not_duplicate_residency(self):
        ledger = PublishedBlocks()
        ledger.accept(0, "a", batch([{"type": "AllBlocksCleared"}, stored()]))
        self.assertTrue(ledger.accept(0, "a", {})["duplicate"])
        result = ledger.accept(1, "b", batch([stored()]))
        self.assertEqual(result["newly_published_blocks"], 0)
        self.assertEqual(result["published_gpu_blocks"], 2)

    def test_gap_and_counter_reset_invalidate_accounting(self):
        ledger = PublishedBlocks()
        ledger.accept(0, "a", batch([{"type": "AllBlocksCleared"}]))
        self.assertFalse(ledger.accept(2, "b", batch([]))["complete_since_clear"])
        ledger.accept(3, "c", batch([{"type": "AllBlocksCleared"}]))
        self.assertFalse(ledger.accept(0, "d", batch([]))["complete_since_clear"])

    def test_remove_keeps_history_without_resident_count(self):
        ledger = PublishedBlocks()
        ledger.accept(0, "a", batch([{"type": "AllBlocksCleared"}, stored()]))
        result = ledger.accept(1, "b", batch([{"type": "BlockRemoved", "block_hashes": [1], "medium": "GPU"}]))
        self.assertEqual(result["published_gpu_blocks"], 1)
        self.assertEqual(ledger.blocks["int:2"]["parent"], "int:1")
        self.assertIn("int:1", ledger.history)

    def test_conflicting_hash_fails_closed(self):
        ledger = PublishedBlocks()
        ledger.accept(0, "a", batch([stored()]))
        bad = stored(); bad["token_ids"][0] = 999
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            ledger.accept(1, "b", batch([bad]))
        self.assertFalse(ledger.complete_since_clear)

    def test_partial_block_rejected(self):
        event = stored(); event["token_ids"].pop()
        with self.assertRaisesRegex(ValueError, "complete 16"):
            PublishedBlocks().accept(0, "a", batch([event]))


if __name__ == "__main__":
    unittest.main()
