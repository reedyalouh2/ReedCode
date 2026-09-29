"""Exercise cache scheduling assumptions with a small, explicit cost model."""

import argparse
from collections import OrderedDict, deque
import copy
import hashlib
import heapq
import json
import math
from pathlib import Path
from statistics import mean


POLICIES = ("off", "eager", "future_timing")
EVENT_ORDER = {"finished": 0, "update": 1, "ready": 2, "arrival": 3, "deadline": 4, "wake": 5}


def block_keys(blocks):
    parent = ""
    keys = []
    for block in blocks:
        parent = hashlib.sha256(json.dumps([parent, block]).encode()).hexdigest()
        keys.append(parent)
    return keys


def validate(workload):
    def number(value, name, positive=False):
        if (type(value) not in (int, float) or not math.isfinite(value)
                or value < 0 or (positive and value == 0)):
            raise ValueError(f"Invalid {name}")

    def blocks(value):
        if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
            raise ValueError("Blocks must be a list of nonempty strings")

    if workload.get("schema_version") != 1 or workload.get("kind") != "synthetic_cost_model":
        raise ValueError("Use a schema_version 1 synthetic_cost_model workload")
    for name in ("cache_blocks", "speculative_block_budget", "speculative_chunk_blocks"):
        if type(workload.get(name)) is not int or workload[name] < 1:
            raise ValueError(f"{name} must be a positive integer")
    for name in ("prefill_block_ms", "horizon_ms"):
        number(workload.get(name), name, positive=True)
    workflows = workload.get("workflows")
    if not isinstance(workflows, list) or not workflows:
        raise ValueError("At least one workflow is required")
    ids = set()
    for workflow in workflows:
        ident = workflow.get("id")
        if not isinstance(ident, str) or not ident or ident in ids:
            raise ValueError("Workflow IDs must be unique")
        ids.add(ident)
        number(workflow.get("arrival_ms", 0), "arrival_ms")
        turns = workflow.get("turns")
        if not isinstance(turns, list) or not turns:
            raise ValueError("Each workflow needs turns")
        for index, turn in enumerate(turns):
            for field in ("prompt_blocks", "generated_blocks"):
                blocks(turn.get(field))
            if not turn["prompt_blocks"]:
                raise ValueError("A prompt must contain at least one block")
            if len(turn["prompt_blocks"] + turn["generated_blocks"]) > workload["cache_blocks"]:
                raise ValueError("Each active request must fit in cache")
            number(turn.get("decode_ms"), "decode_ms", positive=True)
            number(turn.get("wait_ms", 0), "wait_ms")
            if index == 0 and (turn.get("intent") or turn.get("updates")):
                raise ValueError("First turns have no preceding tool wait")
            blocks(turn.get("intent", []))
            updates = turn.get("updates", [])
            last = -1
            for update in updates:
                at = update.get("at_ms")
                number(at, "update at_ms")
                if at < last or at > turn.get("wait_ms", 0):
                    raise ValueError("Updates must be ordered and occur within the tool wait")
                last = at
                if set(update) not in ({"at_ms", "prefix_blocks"}, {"at_ms", "cancel"}):
                    raise ValueError("An update must revise the prefix or cancel")
                if "prefix_blocks" in update:
                    blocks(update["prefix_blocks"])
                elif update["cancel"] is not True:
                    raise ValueError("cancel must be true")
            candidates = [turn.get("intent", [])] + [u["prefix_blocks"] for u in updates if "prefix_blocks" in u]
            if any(len(p) > workload["cache_blocks"] for p in candidates):
                raise ValueError("Preparation must fit in cache")
    return workload


class Simulation:
    def __init__(self, workload, policy):
        self.workload = copy.deepcopy(validate(workload))
        if policy not in POLICIES:
            raise ValueError("Unknown policy")
        self.policy = policy
        self.now = 0.0
        self.events = []
        self.serial = 0
        self.cache = OrderedDict()
        self.queue = deque()
        self.busy = False
        self.intents = {}
        self.states = {}
        self.trace = []
        self.stats = {"demand_compute_ms": 0.0, "speculative_compute_ms": 0.0,
                      "speculative_blocks_computed": 0, "useful_speculative_blocks": 0,
                      "ordinary_cached_blocks": 0, "cache_block_ms": 0.0,
                      "peak_cache_blocks": 0, "obsolete_blocks_completed": 0,
                      "late_blocks_completed": 0}

    def event(self, at, action, *args):
        self.serial += 1
        heapq.heappush(self.events, (at, EVENT_ORDER[action], self.serial, action, args))

    def log(self, action, **data):
        self.trace.append({"at_ms": self.now, "event": action, **data})

    def advance(self, at):
        self.stats["cache_block_ms"] += (at - self.now) * len(self.cache)
        self.now = at

    def cached_prefix(self, keys):
        for i, key in enumerate(keys):
            if key not in self.cache or self.cache[key] == "pending":
                return i
        return len(keys)

    def reserve(self, keys):
        protected = set(keys)
        new = sum(key not in self.cache for key in keys)
        while len(self.cache) + new > self.workload["cache_blocks"]:
            victim = next(key for key in self.cache if key not in protected)
            del self.cache[victim]
        for key in keys:
            if key not in self.cache:
                self.cache[key] = "pending"
            self.cache.move_to_end(key)
        self.stats["peak_cache_blocks"] = max(self.stats["peak_cache_blocks"], len(self.cache))

    def start_turn(self, ident, index):
        state = self.states[ident]
        if state["status"] != "running":
            return
        turn = state["workflow"]["turns"][index]
        wait = turn.get("wait_ms", 0)
        ready = self.now + wait
        self.event(ready, "ready", ident, index)
        if index:
            intent = {"index": index, "revision": 0, "keys": block_keys(turn.get("intent", [])),
                      "ready_at": ready, "active": True, "eligible_at": self.now}
            updates = turn.get("updates", [])
            if self.policy == "future_timing":
                intent["eligible_at"] = self.now + max((u["at_ms"] for u in updates), default=0)
                intent["will_cancel"] = any(u.get("cancel") for u in updates)
            self.intents[ident] = intent
            for update in updates:
                self.event(self.now + update["at_ms"], "update", ident, index, update)
            self.schedule_wakeup(intent)

    def schedule_wakeup(self, intent):
        if self.policy == "future_timing" and intent["active"]:
            missing = len(intent["keys"]) - self.cached_prefix(intent["keys"])
            at = max(self.now, intent["eligible_at"],
                     intent["ready_at"] - missing * self.workload["prefill_block_ms"])
            if self.now < at < intent["ready_at"]:
                self.event(at, "wake")

    def handle(self, action, args):
        if action == "arrival":
            self.start_turn(args[0], 0)
        elif action == "deadline":
            ident = args[0]
            if self.states[ident]["status"] == "running":
                self.states[ident]["status"] = "timeout"
                if ident in self.intents:
                    self.intents[ident]["active"] = False
                self.log("deadline", workflow=ident)
        elif action == "ready":
            ident, index = args
            if self.states[ident]["status"] == "running":
                if ident in self.intents:
                    self.intents[ident]["active"] = False
                self.queue.append((ident, index, self.now))
                self.log("request_ready", workflow=ident, turn=index)
        elif action == "update":
            ident, index, update = args
            intent = self.intents.get(ident)
            if not intent or intent["index"] != index or self.states[ident]["status"] != "running":
                return
            intent["revision"] += 1
            if update.get("cancel"):
                intent["active"] = False
                self.states[ident]["status"] = "cancelled"
                self.log("cancel", workflow=ident)
            else:
                intent["keys"] = block_keys(update["prefix_blocks"])
                self.log("revision", workflow=ident, revision=intent["revision"])
                self.schedule_wakeup(intent)
        elif action == "finished":
            job = args[0]
            self.busy = False
            if job["kind"] == "speculative":
                current = self.intents[job["workflow"]]
                obsolete = (self.states[job["workflow"]]["status"] in {"cancelled", "timeout"}
                            or current["revision"] != job["revision"])
                if obsolete:
                    self.stats["obsolete_blocks_completed"] += len(job["computed"])
                if self.now > current["ready_at"]:
                    self.stats["late_blocks_completed"] += len(job["computed"])
                for key in job["computed"]:
                    self.cache[key] = "speculative"
                self.log("preparation_finished", workflow=job["workflow"],
                         blocks=len(job["computed"]), obsolete=obsolete)
            else:
                for key in job["keys"]:
                    self.cache[key] = "ordinary"
                ident, index = job["workflow"], job["index"]
                state = self.states[ident]
                state["turns"].append({"turn": index, "ready_ms": job["ready"],
                                       "completed_ms": self.now, "cached_blocks": job["cached"]})
                self.log("request_finished", workflow=ident, turn=index)
                if state["status"] != "running":
                    return
                if index + 1 == len(state["workflow"]["turns"]):
                    state["status"] = "completed"
                    state["completion_ms"] = self.now - state["workflow"].get("arrival_ms", 0)
                else:
                    self.start_turn(ident, index + 1)

    def dispatch(self):
        if self.busy:
            return
        while self.queue:
            ident, index, ready = self.queue.popleft()
            if self.states[ident]["status"] != "running":
                continue
            turn = self.states[ident]["workflow"]["turns"][index]
            prompt = block_keys(turn["prompt_blocks"])
            keys = block_keys(turn["prompt_blocks"] + turn["generated_blocks"])
            cached = self.cached_prefix(prompt)
            for key in prompt[:cached]:
                if self.cache[key] == "speculative":
                    self.stats["useful_speculative_blocks"] += 1
                else:
                    self.stats["ordinary_cached_blocks"] += 1
                self.cache[key] = "ordinary"
            cost = (len(prompt) - cached) * self.workload["prefill_block_ms"] + turn["decode_ms"]
            self.reserve(keys)
            job = {"kind": "demand", "workflow": ident, "index": index, "keys": keys,
                   "ready": ready, "cached": cached}
            self.stats["demand_compute_ms"] += cost
            self.busy = True
            self.log("request_started", workflow=ident, turn=index, cost_ms=cost, cached_blocks=cached)
            self.event(self.now + cost, "finished", job)
            return
        if self.policy == "off":
            return
        remaining = self.workload["speculative_block_budget"] - self.stats["speculative_blocks_computed"]
        if remaining <= 0:
            return
        for ident, intent in self.intents.items():
            if not intent["active"] or self.states[ident]["status"] != "running":
                continue
            keys = intent["keys"]
            cached = self.cached_prefix(keys)
            if cached == len(keys):
                continue
            if self.policy == "future_timing":
                if intent.get("will_cancel") or self.now < intent["eligible_at"]:
                    continue
                begin = intent["ready_at"] - (len(keys) - cached) * self.workload["prefill_block_ms"]
                if self.now < begin:
                    self.schedule_wakeup(intent)
                    continue
            count = min(len(keys) - cached, remaining, self.workload["speculative_chunk_blocks"])
            target = keys[:cached + count]
            computed = keys[cached:cached + count]
            self.reserve(target)
            cost = count * self.workload["prefill_block_ms"]
            self.stats["speculative_blocks_computed"] += count
            self.stats["speculative_compute_ms"] += cost
            self.busy = True
            self.log("preparation_started", workflow=ident, revision=intent["revision"], blocks=count)
            self.event(self.now + cost, "finished", {"kind": "speculative", "workflow": ident,
                       "revision": intent["revision"], "computed": computed})
            return

    def run(self):
        horizon = self.workload["horizon_ms"]
        observation_end = max(w.get("arrival_ms", 0) for w in self.workload["workflows"]) + horizon
        for workflow in self.workload["workflows"]:
            self.states[workflow["id"]] = {"workflow": workflow, "status": "running", "turns": []}
            self.event(workflow.get("arrival_ms", 0), "arrival", workflow["id"])
            self.event(workflow.get("arrival_ms", 0) + horizon, "deadline", workflow["id"])
        while self.events and self.events[0][0] <= observation_end:
            at = self.events[0][0]
            self.advance(at)
            # Apply every event at this instant before choosing more GPU work.
            while self.events and self.events[0][0] == at:
                _, _, _, action, args = heapq.heappop(self.events)
                self.handle(action, args)
            self.dispatch()
        self.advance(observation_end)
        rows = []
        for ident, state in self.states.items():
            rows.append({"workflow": ident, "status": "timeout" if state["status"] == "running" else state["status"],
                         "capped_completion_ms": state.get("completion_ms", horizon), "turns": state["turns"]})
        self.stats["unconsumed_speculative_blocks"] = (self.stats["speculative_blocks_computed"]
                                                       - self.stats["useful_speculative_blocks"])
        return {"policy": self.policy, "workflows": rows, "metrics": self.stats,
                "mean_capped_completion_ms": mean(r["capped_completion_ms"] for r in rows), "events": self.trace}


def run_study(workload):
    results = [Simulation(workload, p).run() for p in POLICIES]
    base = results[0]["mean_capped_completion_ms"]
    return {"schema_version": 1, "kind": "synthetic_cost_model", "gpu_measurements": False,
            "workload_sha256": hashlib.sha256(json.dumps(workload, sort_keys=True).encode()).hexdigest(),
            "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "assumptions": ["One serialized GPU resource; fixed synthetic service costs; no batching.",
                            "Demand has dispatch priority; running preparation chunks cannot be preempted.",
                            "Ordinary decoding populates prompt and generated full blocks.",
                            "Cache starts empty in every condition; LRU eviction; one common speculation budget.",
                            "Future timing knows tool duration and revision/cancellation times, never future block contents.",
                            "Future timing is a diagnostic heuristic, not an optimal oracle or a GPU performance bound.",
                            "Synthetic full blocks omit tokenizer boundaries; use the separate pinned prefix audit.",
                            "Reserved and resident cache blocks both count toward capacity and occupancy.",
                            "Charged compute includes the full cost of chunks admitted before the horizon."],
            "results": [{**r, "mean_difference_from_off_ms": r["mean_capped_completion_ms"] - base} for r in results]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workload", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run_study(json.loads(args.workload.read_text()))
    with args.output.open("x") as file:
        file.write(json.dumps(result, indent=2) + "\n")
    print("Synthetic cost model. No GPU performance estimate.")
    for row in result["results"]:
        print(f"{row['policy']}: {row['mean_capped_completion_ms']:.3f} model-ms mean completion; "
              f"{row['metrics']['useful_speculative_blocks']} useful prepared blocks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
