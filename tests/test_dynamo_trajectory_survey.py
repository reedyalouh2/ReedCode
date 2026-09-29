from copy import deepcopy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / "experiments/dynamo-prefix"
ARCHIVE = REPO / "experiments/dynamo-20260928/raw-records.tar.gz"
spec = importlib.util.spec_from_file_location("trajectory_survey_audit", ROOT / "audit.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
spec = importlib.util.spec_from_file_location("trajectory_survey", ROOT / "survey.py")
survey = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"audit": audit}):
    spec.loader.exec_module(survey)
TOKENIZER = Path(os.environ.get("REEDCODE_QWEN_TOKENIZER", "/tmp/reedcode-prefix-tokenizer/tokenizer.json"))
HAS_PREFIX_DEPS = all(importlib.util.find_spec(name) is not None
                      for name in ("xxhash", "jinja2", "tokenizers"))


class TokenFixture:
    def __init__(self, candidate):
        self.prepared = candidate

    def render(self, request, messages, *, generation):
        return request["fixture_token_ids"]

    def tokens(self, rendered):
        return rendered

    def candidate(self, request, assistant, *, block_size):
        return {"prepared_token_ids": self.prepared}


def transition_fixture(initial_ids, next_ids):
    request = {"model": "fixture", "messages": [{"role": "user", "content": "Read the file"}],
               "tools": [{"type": "function", "function": {"name": "read_file"}}],
               "fixture_token_ids": initial_ids}
    message = {"role": "assistant", "tool_calls": [
        {"id": "call-one", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]}
    following = deepcopy(request)
    following["fixture_token_ids"] = next_ids
    following["messages"] += [message, {"role": "tool", "tool_call_id": "call-one", "content": "file contents"}]
    return {
        "current": {"turn": 1, "request": request},
        "assistant": {"message": message, "elapsed_ms": 10},
        "following": {"turn": 2, "request": following, "elapsed_ms": 31},
        "inference": {"response_status": "completed", "finish_reason": "tool_calls", "output_tokens": 3},
        "next_inference": {"cached_input_tokens": 2},
        "tools": [{"duration_ms": 20, "call_id": "call-one", "tool": "read_file"}],
    }


class SurveyTransitionTests(unittest.TestCase):
    def test_input_mismatch_makes_reuse_bound_exact(self):
        args = transition_fixture([1, 2, 3, 4], [1, 2, 9, 10, 11, 12, 13, 14, 15])
        result = survey.transition(TokenFixture([1, 2, 9, 10, 11, 12]), **args, block_size=2)
        self.assertEqual(result["prior_input_full_block_tokens_lost"], 2)
        self.assertEqual((result["ordinary_decode_reusable_tokens_lower"],
                          result["ordinary_decode_reusable_tokens_upper"]), (2, 2))
        self.assertEqual((result["additional_preparable_tokens_lower"],
                          result["additional_preparable_tokens_upper"]), (4, 4))
        self.assertEqual(result["tokens_after_preparable_prefix"], 3)
        self.assertIs(result["raw_sampled_output_ids_available"], False)

    def test_unknown_output_produces_interval_even_with_compatible_candidate(self):
        args = transition_fixture([1, 2, 3, 4], list(range(1, 11)))
        result = survey.transition(TokenFixture(list(range(1, 9))), **args, block_size=2)
        self.assertEqual((result["ordinary_decode_reusable_tokens_lower"],
                          result["ordinary_decode_reusable_tokens_upper"]), (4, 6))
        self.assertEqual((result["additional_preparable_tokens_lower"],
                          result["additional_preparable_tokens_upper"]), (2, 4))

    def test_changed_history_schema_or_missing_tool_output_invalidates_transition(self):
        base = transition_fixture([1, 2], [1, 2, 3, 4])
        changed_history = deepcopy(base)
        changed_history["following"]["request"]["messages"][0]["content"] = "Compacted history"
        changed_schema = deepcopy(base)
        changed_schema["following"]["request"]["tools"][0]["function"]["name"] = "write_file"
        missing_output = deepcopy(base)
        missing_output["following"]["request"]["messages"].pop()
        for args in (changed_history, changed_schema, missing_output):
            with self.subTest(args=args), self.assertRaises(ValueError):
                survey.transition(TokenFixture([1, 2]), **args, block_size=2)

    def test_incompatible_candidate_cannot_be_counted_as_preparable(self):
        args = transition_fixture([1, 2], [1, 2, 3, 4])
        with self.assertRaisesRegex(ValueError, "exact prefix"):
            survey.transition(TokenFixture([1, 99]), **args, block_size=2)

    def test_cut_off_response_cannot_supply_known_continuation(self):
        args = transition_fixture([1, 2], [1, 2, 3, 4])
        args["inference"].update(response_status="incomplete", finish_reason="length")
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            survey.transition(TokenFixture([1, 2]), **args, block_size=2)

    def test_invalid_output_count_cannot_bound_decode_reuse(self):
        for count in (-1, None, True, 3.5):
            args = transition_fixture([1, 2], [1, 2, 3, 4])
            args["inference"]["output_tokens"] = count
            with self.subTest(count=count), self.assertRaisesRegex(ValueError, "output token count"):
                survey.transition(TokenFixture([1, 2]), **args, block_size=2)

    def test_tool_wait_requires_completed_calls_with_valid_durations(self):
        base = transition_fixture([1, 2], [1, 2, 3, 4])
        for tools in ([], [{"duration_ms": 2, "call_id": "wrong", "tool": "read_file"}],
                      [{"duration_ms": -1, "call_id": "call-one", "tool": "read_file"}],
                      [{"duration_ms": float("nan"), "call_id": "call-one", "tool": "read_file"}]):
            args = deepcopy(base)
            args["tools"] = tools
            with self.subTest(tools=tools), self.assertRaises(ValueError):
                survey.transition(TokenFixture([1, 2]), **args, block_size=2)


class SurveyIntegrityTests(unittest.TestCase):
    def test_archive_payload_must_match_its_recorded_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "records.tar.gz"
            checksums = json.dumps({"record.json": hashlib.sha256(b"original").hexdigest()}).encode()
            with tarfile.open(path, "w:gz") as archive:
                for name, data in (("sha256.json", checksums), ("record.json", b"changed")):
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                survey.read_records(path)

    def test_duplicate_trace_or_server_identity_cannot_silently_overwrite_evidence(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            survey.by_turn([{"type": "request", "turn": 1}, {"type": "request", "turn": 1}], "request")
        row = {"event": {"event_type": "request_end", "request": {"request_id": "same-id"}}}
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            survey.request_end_index((json.dumps(row) + "\n" + json.dumps(row)).encode())


class SurveyReplayEligibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tarfile.open(ARCHIVE) as archive:
            manifest = json.load(archive.extractfile("pilot/manifest.json"))
            cls.recorded_run = manifest["runs"][0]
            cls.events = [json.loads(line) for line in archive.extractfile("pilot/" + cls.recorded_run["trace"])]

    def reasons(self, events=None, run=None, verified=None):
        events = self.events if events is None else events
        requests, inference, assistants = (survey.by_turn(events, kind)
                                           for kind in ("request", "inference", "assistant_message"))
        numbers = sorted(requests)
        pairs = [{"from_turn": before, "to_turn": after, "status": "verified"}
                 for before, after in zip(numbers, numbers[1:])]
        return survey.replay_eligibility(events, requests, inference, assistants,
                                         self.recorded_run if run is None else run,
                                         set(requests) if verified is None else verified, pairs)

    def test_complete_failed_verifier_trajectory_is_still_eligible(self):
        self.assertEqual(self.reasons(), [])
        self.assertEqual(self.reasons(run=dict(self.recorded_run, reward=0.0)), [])

    def test_missing_terminal_summary_or_assistant_prevents_export(self):
        no_summary = [e for e in self.events if e["type"] != "task_summary"]
        last_turn = max(e["turn"] for e in self.events if e["type"] == "request")
        no_assistant = [e for e in self.events if not (e["type"] == "assistant_message" and e["turn"] == last_turn)]
        self.assertTrue(self.reasons(events=no_summary))
        self.assertTrue(self.reasons(events=no_assistant))

    def test_output_limit_at_final_request_prevents_export_even_if_input_verified(self):
        events = deepcopy(self.events)
        last = max(e["turn"] for e in events if e["type"] == "request")
        for event in events:
            if event["type"] == "inference" and event["turn"] == last:
                event.update(response_status="incomplete", finish_reason="length", output_limit_hit=True)
        self.assertTrue(self.reasons(events=events))

    def test_missing_first_turn_and_orphan_inference_prevent_export(self):
        missing = [e for e in self.events if e.get("turn") != 1]
        self.assertTrue(self.reasons(events=missing))
        extra = deepcopy(self.events)
        extra.append({"type": "inference", "turn": 99, "response_status": "completed", "finish_reason": "stop"})
        self.assertTrue(self.reasons(events=extra))

    def test_unverified_request_or_inconsistent_terminal_counts_prevent_export(self):
        self.assertTrue(self.reasons(verified={1}))
        changed = deepcopy(self.events)
        for event in changed:
            if event["type"] == "task_summary":
                event["model_calls"] += 1
        self.assertTrue(self.reasons(events=changed))
        self.assertTrue(self.reasons(run=dict(self.recorded_run, exception_type="TimeoutError")))


@unittest.skipUnless(TOKENIZER.exists() and HAS_PREFIX_DEPS,
                     "Download the pinned tokenizer and install prefix requirements")
class ArchivedTrajectorySurveyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report, cls.workload = survey.survey(TOKENIZER)

    def test_archive_coverage_and_input_continuity_match_independent_trace_counts(self):
        coverage = self.report["coverage"]
        self.assertEqual((coverage["runs"], coverage["requests"], coverage["continuations"]), (10, 52, 42))
        self.assertEqual((coverage["verified_requests"], coverage["verified_continuations"]), (52, 42))
        continuity = self.report["input_continuity"]
        self.assertEqual(continuity["prior_complete_block_tokens"], 102592)
        self.assertEqual(continuity["shared_complete_block_tokens"], 102512)
        self.assertEqual(self.report["recorded_cache"]["initial_requests_with_hits"], 9)
        self.assertEqual(self.report["recorded_cache"]["continuations_above_previous_input_prefix"], 15)
        self.assertAlmostEqual(self.report["summary"]["tool_wait_ms"]["median"], 237.885)
        self.assertEqual(self.report["new_model_calls"], 0)
        self.assertIs(self.report["new_gpu_measurements"], False)

    def test_export_preserves_every_trajectory_and_matches_replay_schema(self):
        from dynamo_replay import validate_workload

        self.assertIs(validate_workload(self.workload), self.workload)
        self.assertEqual(len(self.workload["workflows"]), 10)
        self.assertEqual(sum(len(w["turns"]) for w in self.workload["workflows"]), 52)
        self.assertTrue(self.report["replay_export"]["complete"])
        for workflow in self.workload["workflows"]:
            self.assertEqual(workflow["turns"][0]["wait_ms"], 0)
            self.assertTrue(all(t["wait_ms"] > 0 for t in workflow["turns"][1:]))


if __name__ == "__main__":
    unittest.main()
