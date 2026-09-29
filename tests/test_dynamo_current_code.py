from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / "experiments/dynamo-upstream/001-speculative-prefill/current-code"
spec = importlib.util.spec_from_file_location("dynamo_current_code", ROOT / "reproduce.py")
current = importlib.util.module_from_spec(spec)
spec.loader.exec_module(current)
HAS_XXHASH = importlib.util.find_spec("xxhash") is not None


class PinnedSourceTests(unittest.TestCase):
    def test_changed_source_is_rejected_before_compilation(self):
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory)
            shutil.copytree(ROOT / "sources", copied / "sources")
            source = copied / "sources/release/speculative_prefill.rs"
            source.write_bytes(source.read_bytes() + b"\n")
            with patch.object(current, "ROOT", copied), self.assertRaisesRegex(
                ValueError, "Pinned upstream source changed"
            ):
                current.read_verified_sources()

    def test_ambiguous_source_fragment_is_rejected(self):
        source = "pub struct Request {}\nend\npub struct Request {}\nend\n"
        with self.assertRaisesRegex(ValueError, "Ambiguous source anchor"):
            current.extract(source, "pub struct Request", "end")


@unittest.skipUnless(HAS_XXHASH and (ROOT / "results.json").exists(),
                     "Run the pinned CPU reproduction and install prefix requirements")
class RuntimeEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        report = json.loads((ROOT / "results.json").read_text())
        cls.rows = next(run["cases"] for run in report["runs"] if run["label"] == "release")
        cls.records = current.survey.read_records(current.survey.ARCHIVE)
        cls.prior = json.loads((ROOT.parent / "results.json").read_text())

    def test_changed_release_normal_prompt_cannot_pass(self):
        rows = deepcopy(self.rows)
        rows[0]["original"]["token_ids"][0] += 1
        with self.assertRaisesRegex(ValueError, "normal-request adapter differs"):
            current.verify_runtime(rows, self.records, self.prior, require_archived_normal=True)

    def test_changed_current_prompt_is_reported_as_a_mismatch(self):
        rows = deepcopy(self.rows)
        rows[0]["followup"]["token_ids"][0] += 1
        result = current.verify_runtime(rows, self.records, self.prior, require_archived_normal=False)
        self.assertEqual(result[0]["comparison"]["normal_runtime_hash_checks"]["followup"]["status"],
                         "mismatch")


if __name__ == "__main__":
    unittest.main()
