import copy
import json
from pathlib import Path
import unittest

from dynamo_headroom import POLICIES, Simulation, block_keys, run_study, validate


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "experiments" / "dynamo-headroom" / "workloads"


def turn(prompt, generated=(), *, decode=1, wait=0, intent=None, updates=()):
    result = {"prompt_blocks": list(prompt), "generated_blocks": list(generated),
              "decode_ms": decode, "wait_ms": wait}
    if intent is not None:
        result["intent"] = list(intent)
    if updates:
        result["updates"] = list(updates)
    return result


def workload(turns, **changes):
    result = {"schema_version": 1, "kind": "synthetic_cost_model",
              "cache_blocks": 12, "speculative_block_budget": 20,
              "speculative_chunk_blocks": 2, "prefill_block_ms": 5,
              "horizon_ms": 100,
              "workflows": [{"id": "agent", "arrival_ms": 0, "turns": turns}]}
    result.update(changes)
    return result


def events(result, kind, ident=None):
    return [e for e in result["events"] if e["event"] == kind
            and (ident is None or e.get("workflow") == ident)]


class HeadroomTests(unittest.TestCase):
    def test_ordinary_decode_leaves_no_quiet_preparation_work(self):
        data = workload([turn(["system", "user"], ["call"]),
                         turn(["system", "user", "call", "tool"], wait=20,
                              intent=["system", "user", "call"])])
        results = run_study(data)["results"]
        self.assertEqual(len({r["mean_capped_completion_ms"] for r in results}), 1)
        for result in results:
            self.assertEqual(result["metrics"]["speculative_blocks_computed"], 0)
            self.assertEqual(result["metrics"]["ordinary_cached_blocks"], 3)

    def test_block_identity_depends_on_every_parent(self):
        first = block_keys(["shared", "left", "same-text"])
        second = block_keys(["shared", "right", "same-text"])
        self.assertEqual(first[0], second[0])
        self.assertNotEqual(first[1], second[1])
        self.assertNotEqual(first[2], second[2])
        self.assertEqual(first, block_keys(["shared", "left", "same-text"]))

    def test_equal_suffix_under_different_parent_is_recomputed(self):
        data = workload([turn(["left", "same-text"]),
                         turn(["right", "same-text"])])
        result = Simulation(data, "off").run()
        self.assertEqual(result["metrics"]["ordinary_cached_blocks"], 0)
        self.assertEqual(result["metrics"]["demand_compute_ms"], 22)

    def test_common_resource_charges_delay_to_other_requests(self):
        data = workload([turn(["initial"]),
                         turn(["new", "context", "tool"], wait=50,
                              intent=["new", "context"])], prefill_block_ms=10)
        data["workflows"].append({"id": "background", "arrival_ms": 12,
                                  "turns": [turn(["unrelated"])]})
        off = Simulation(data, "off").run()
        eager = Simulation(data, "eager").run()
        self.assertEqual(events(off, "request_started", "background")[0]["at_ms"], 12)
        self.assertEqual(events(eager, "request_started", "background")[0]["at_ms"], 31)
        self.assertEqual(eager["metrics"]["speculative_compute_ms"], 20)

    def test_speculation_stops_at_common_block_budget(self):
        data = workload([turn(["initial"]),
                         turn(["a", "b", "c", "d", "e", "tool"], wait=60,
                              intent=["a", "b", "c", "d", "e"])],
                        speculative_block_budget=3)
        for policy in POLICIES:
            result = Simulation(data, policy).run()
            self.assertLessEqual(result["metrics"]["speculative_blocks_computed"], 3)
            self.assertEqual(result["metrics"]["speculative_compute_ms"],
                             result["metrics"]["speculative_blocks_computed"] * 5)
        self.assertEqual(Simulation(data, "eager").run()["metrics"]["speculative_blocks_computed"], 3)

    def test_tool_wait_starts_after_preceding_completion(self):
        data = workload([turn(["initial"], decode=8),
                         turn(["initial", "tool"], wait=17, intent=["initial"])])
        for policy in POLICIES:
            result = Simulation(data, policy).run()
            finished = events(result, "request_finished", "agent")[0]["at_ms"]
            ready = events(result, "request_ready", "agent")[1]["at_ms"]
            self.assertEqual(ready - finished, 17)

    def test_future_timing_cannot_read_unrevealed_context(self):
        data = workload([turn(["initial"]),
                         turn(["revealed", "context", "unknown-tool-output"], wait=30,
                              intent=[], updates=[{"at_ms": 25,
                                                   "prefix_blocks": ["revealed", "context"]}])])
        result = Simulation(data, "future_timing").run()
        reveal = events(result, "revision")[0]["at_ms"]
        preparations = events(result, "preparation_started")
        self.assertTrue(preparations)
        self.assertTrue(all(e["at_ms"] >= reveal for e in preparations))
        self.assertEqual(result["metrics"]["speculative_blocks_computed"], 2)
        self.assertEqual(result["metrics"]["useful_speculative_blocks"], 2)

    def test_late_valid_preparation_is_not_obsolete(self):
        data = workload([turn(["initial"]),
                         turn(["new", "context", "tool"], wait=5,
                              intent=["new", "context"])], prefill_block_ms=10)
        result = Simulation(data, "eager").run()
        self.assertEqual(result["metrics"]["useful_speculative_blocks"], 2)
        self.assertEqual(result["metrics"]["obsolete_blocks_completed"], 0)
        ready = events(result, "request_ready")[1]["at_ms"]
        finished = events(result, "preparation_finished")[0]["at_ms"]
        started = events(result, "request_started")[1]["at_ms"]
        self.assertGreater(finished, ready)
        self.assertGreaterEqual(started, finished)

    def test_cancelled_inflight_work_is_charged_and_cannot_revive(self):
        data = workload([turn(["initial"]),
                         turn(["new", "context", "tool"], wait=20,
                              intent=["new", "context"],
                              updates=[{"at_ms": 3, "cancel": True},
                                       {"at_ms": 4, "prefix_blocks": ["replacement"]}])])
        result = Simulation(data, "eager").run()
        self.assertEqual(result["metrics"]["speculative_compute_ms"], 10)
        self.assertEqual(result["metrics"]["obsolete_blocks_completed"], 2)
        self.assertEqual(result["workflows"][0]["status"], "cancelled")
        self.assertEqual(result["workflows"][0]["capped_completion_ms"], 100)
        self.assertEqual(len(events(result, "request_started")), 1)
        self.assertFalse(events(result, "revision"))

    def test_revision_charges_old_work_and_hides_replacement_until_event(self):
        data = workload([turn(["initial"]),
                         turn(["revised", "context", "tool"], wait=40,
                              intent=["old", "context"],
                              updates=[{"at_ms": 3,
                                        "prefix_blocks": ["revised", "context"]}])])
        eager = Simulation(data, "eager").run()
        future = Simulation(data, "future_timing").run()
        self.assertEqual(eager["metrics"]["obsolete_blocks_completed"], 2)
        self.assertEqual(eager["metrics"]["speculative_blocks_computed"], 4)
        self.assertEqual(future["metrics"]["speculative_blocks_computed"], 2)
        self.assertTrue(all(e["revision"] == 1 for e in events(future, "preparation_started")))

    def test_cancelling_one_owner_keeps_shared_preparation(self):
        data = json.loads((FIXTURES / "shared-prefix.json").read_text())
        result = Simulation(data, "eager").run()
        self.assertEqual(result["metrics"]["speculative_blocks_computed"], 1)
        self.assertEqual(result["metrics"]["useful_speculative_blocks"], 1)
        states = {row["workflow"]: row["status"] for row in result["workflows"]}
        self.assertEqual(states, {"cancelled-owner": "cancelled", "consumer": "completed"})

    def test_capacity_counts_the_whole_active_request(self):
        data = workload([turn(["one", "two"], ["three", "four"])], cache_blocks=4)
        result = Simulation(data, "off").run()
        self.assertEqual(result["metrics"]["peak_cache_blocks"], 4)
        self.assertEqual(result["metrics"]["cache_block_ms"], 400)
        too_large = copy.deepcopy(data)
        too_large["workflows"][0]["turns"][0]["generated_blocks"].append("five")
        with self.assertRaises(ValueError):
            validate(too_large)

    def test_deadline_is_relative_to_workflow_arrival(self):
        data = workload([turn(["one"], decode=4)], horizon_ms=10)
        data["workflows"][0]["arrival_ms"] = 100
        result = Simulation(data, "off").run()
        self.assertEqual(result["workflows"][0]["status"], "completed")
        self.assertEqual(result["workflows"][0]["capped_completion_ms"], 9)

    def test_inflight_at_deadline_keeps_full_charged_cost(self):
        data = workload([turn(["one"], decode=20)], horizon_ms=10)
        result = Simulation(data, "off").run()
        self.assertEqual(result["workflows"][0]["status"], "timeout")
        self.assertEqual(result["workflows"][0]["capped_completion_ms"], 10)
        self.assertEqual(result["metrics"]["demand_compute_ms"], 25)
        self.assertEqual(result["metrics"]["cache_block_ms"], 10)

    def test_completion_exactly_at_deadline_is_retained(self):
        data = workload([turn(["one"], decode=5)], horizon_ms=10)
        result = Simulation(data, "off").run()
        self.assertEqual(result["workflows"][0]["status"], "completed")
        self.assertEqual(result["workflows"][0]["capped_completion_ms"], 10)

    def test_diagnostic_fixtures_are_deterministic_and_capacity_bounded(self):
        fixtures = sorted(FIXTURES.glob("*.json"))
        self.assertEqual(len(fixtures), 4)
        for fixture in fixtures:
            with self.subTest(fixture=fixture.name):
                data = json.loads(fixture.read_text())
                first = run_study(data)
                self.assertEqual(first, run_study(data))
                self.assertIs(first["gpu_measurements"], False)
                for result in first["results"]:
                    metrics = result["metrics"]
                    self.assertLessEqual(metrics["peak_cache_blocks"], data["cache_blocks"])
                    self.assertLessEqual(metrics["speculative_blocks_computed"], data["speculative_block_budget"])
                    self.assertEqual(metrics["unconsumed_speculative_blocks"],
                                     metrics["speculative_blocks_computed"] - metrics["useful_speculative_blocks"])


if __name__ == "__main__":
    unittest.main()
