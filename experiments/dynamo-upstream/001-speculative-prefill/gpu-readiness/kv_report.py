"""Classify published full KV blocks for one controlled replay trial."""

import argparse
import base64
import importlib.util
import json
from pathlib import Path

from prepare import REPO, ROOT, sha
from replay import selected
from verify import read_gzip, verify


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


collector = module(ROOT.parents[1] / "gpu-readiness/collect_kv.py", "prefill_collector")
identity_module = module(ROOT.parent / "kv-footprint/reproduce.py", "prefill_identity")
metrics_reader = module(REPO / "server_metrics.py", "prefill_metric_reader")
COMPUTE_COUNTER = "vllm:prompt_tokens_by_source_total"
COMPUTE_CREATED = "vllm:prompt_tokens_by_source_created"
metrics_reader.NAMES = metrics_reader.NAMES | {COMPUTE_COUNTER, COMPUTE_CREATED}


def computed_prefill(request, directory):
    observations = request.get("observations") or {}
    before, after = observations.get("before"), observations.get("after")
    unknown = {"value": None, "status": "missing_observation"}
    if not before or not after or observations.get("error"):
        return unknown
    if not before.get("identity_fingerprint") or before["identity_fingerprint"] != after.get("identity_fingerprint"):
        return {"value": None, "status": "identity_changed_or_unknown"}
    parsed = []
    wanted = {"model_name": "Qwen/Qwen3-8B", "engine": "0", "source": "local_compute"}
    for observation in (before, after):
        names = [name for name in observation["files"] if name.endswith(".metrics.txt")]
        if len(names) != 1:
            return unknown
        raw = (directory / names[0]).read_bytes()
        if sha(raw) != observation["files"][names[0]]:
            raise ValueError("Metrics snapshot hash differs from its observation")
        sample = metrics_reader.parse_metrics(raw.decode(), "Qwen/Qwen3-8B")
        for aliases in (metrics_reader.GAUGES["requests_running"], metrics_reader.GAUGES["requests_waiting"]):
            values = sample.get(aliases[0])
            if not values or any(dict(key).get("engine") != "0" or value != 0 for key, value in values.items()):
                return {"value": None, "status": "idle_single_engine_unconfirmed"}
        values = sample.get(COMPUTE_COUNTER, {})
        matches = [labels for labels in values if all(dict(labels).get(key) == value for key, value in wanted.items())]
        if not matches:
            return {"value": None, "status": "local_compute_counter_missing"}
        if len(matches) != 1:
            return {"value": None, "status": "local_compute_counter_ambiguous"}
        selected_labels = matches[0]
        sample[COMPUTE_COUNTER] = {selected_labels: values[selected_labels]}
        created = sample.get(COMPUTE_CREATED, {})
        sample[COMPUTE_CREATED] = {selected_labels: created[selected_labels]} if selected_labels in created else {}
        parsed.append(sample)
    finished = metrics_reader._delta(parsed, metrics_reader.COUNTERS["requests_finished"])
    if finished["status"] != "ok" or finished["value"] != 1:
        return {"value": None, "status": "one_finished_request_unconfirmed"}
    result = metrics_reader._delta(parsed, (COMPUTE_COUNTER,))
    result["metric"] = COMPUTE_COUNTER
    result["labels"] = dict(next(iter(parsed[0][COMPUTE_COUNTER])))
    return result


def events(path, allow_partial=False):
    import msgpack

    result = []
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not line.endswith(b"\n"):
            if allow_partial and index == len(lines) - 1:
                break
            raise ValueError("KV capture ends in a partial record")
        row = json.loads(line)
        frames = [base64.b64decode(value, validate=True) for value in row["frames_base64"]]
        if len(frames) != 3 or len(frames[1]) != 8:
            raise ValueError("Unexpected KV multipart frame layout")
        sequence, digest = int.from_bytes(frames[1], "big"), sha(frames[2])
        if row.get("sequence") != sequence or row.get("payload_sha256") != digest or "decode_error_type" in row:
            raise ValueError("KV collector decode failed or its saved frame metadata disagrees")
        result.append({**row, "topic": frames[0].decode(), "batch": msgpack.unpackb(frames[2], raw=False)})
    return result


def resolve_blocks(history, identity):
    resolved, unresolved = {}, {}
    def resolve(key):
        pending, seen = [], set()
        while key is not None and key not in resolved:
            if key in seen or key not in history:
                raise ValueError("cycle_or_missing_parent")
            seen.add(key)
            row, tokens = history[key], history[key]["token_ids"]
            if len(tokens) != 16 or any(type(t) is not int or not 0 <= t <= 0xffffffff for t in tokens):
                raise ValueError("invalid_block_tokens")
            pending.append((key, tokens))
            key = row["parent"]
        parent = 0 if key is None else resolved[key]
        for key, tokens in reversed(pending):
            node = (parent, tuple(tokens))
            resolved[key] = identity.nodes.setdefault(node, len(identity.nodes) + 1)
            parent = resolved[key]
        return parent

    for key in history:
        try:
            resolve(key)
        except ValueError as error:
            unresolved[key] = str(error)
    return resolved, unresolved


def classify(ledger, identity, real, warm, current, total_tokens):
    resolved, unresolved = resolve_blocks(ledger.history, identity)
    counts = dict(shared_real_and_warm=0, real_only=0, warmup_only=0, other_known=0, unresolved_ancestry=0)
    older_real, canonical = 0, set()
    for key in ledger.blocks:
        if key not in resolved:
            counts["unresolved_ancestry"] += 1
            continue
        node = resolved[key]
        canonical.add(node)
        if node in real and node in warm:
            counts["shared_real_and_warm"] += 1
        elif node in real:
            counts["real_only"] += 1
        elif node in warm:
            counts["warmup_only"] += 1
        else:
            counts["other_known"] += 1
        older_real += node in real and node not in current
    if sum(counts.values()) != len(ledger.blocks):
        raise ValueError("Published block classification did not partition the set")
    compatible = ledger.complete_since_clear and not counts["unresolved_ancestry"]
    return {"counts": counts, "published_block_entries": len(ledger.blocks),
            "canonical_prefixes": len(canonical), "older_real_entries_outside_current_input": older_real,
            "current_real_tokens": total_tokens, "current_real_full_blocks": len(current),
            "warmup_only_to_current_full_blocks": counts["warmup_only"] / len(current) if compatible and current else None,
            "warmup_only_to_current_tokens": 16 * counts["warmup_only"] / total_tokens if compatible and total_tokens else None,
            "attributable": compatible, "unresolved": {k: v for k, v in unresolved.items() if k in ledger.blocks}}


def report(trial_dir, wire_path):
    verify()
    trial = json.loads((trial_dir / "trial.json").read_text())
    run = json.loads((trial_dir / "replay/run.json").read_text())
    for name, expected in run["files"].items():
        path = (trial_dir / "replay" / name).resolve()
        if not path.is_relative_to((trial_dir / "replay").resolve()) or sha(path.read_bytes()) != expected:
            raise ValueError("Replay evidence hash changed: " + name)
    requests = [json.loads(line) for line in (trial_dir / "replay/requests.jsonl").read_text().splitlines()]
    config = json.loads((trial_dir / "kv-config.json").read_text())
    plan = read_gzip(ROOT / "fixtures" / f"{run['session']}-replay.json.gz")
    rows = selected(plan, run["condition"])
    if trial.get("status") != "replay_completed" or run.get("status") != "http_replay_completed" or len(rows) != len(requests):
        raise ValueError("Trial is incomplete; preserve it without a completed-trial estimate")
    if any(row["id"] != got["id"] for row, got in zip(rows, requests)):
        raise ValueError("Replay order differs from frozen inputs")
    wire = json.loads(wire_path.read_text())
    timed = bool(run.get("hostname")) and run["hostname"] == config.get("hostname")
    first_sent, last_ended = min(r["started_unix"] for r in requests), max(r["ended_unix"] for r in requests)
    received = []
    for row in wire["requests"]:
        if row.get("kind", "model") != "model" or row["input_token_ids"] == [42]:
            continue
        stamp = row["trace_headers"].get("x-frontend-send-ts-ns")
        if not isinstance(stamp, str) or not stamp.isdecimal():
            raise ValueError("Runtime send timestamp is unavailable for trial selection")
        if not timed or first_sent <= int(stamp) / 1e9 <= last_ended:
            received.append(row)
    if len(received) != len(rows):
        raise ValueError("Model request count in the replay interval differs from the frozen trial")
    links = []
    for expected, sent, actual in zip(rows, requests, received):
        if expected["payload"]["prompt"] != actual["input_token_ids"] or not actual.get("complete"):
            raise ValueError("Backend token arrays or completion differ from the frozen replay")
        bridge = actual.get("http_link")
        if bridge is not None and bridge["http_request_id"] != sent["request_id"]:
            raise ValueError("Frontend observer-header linkage disagrees with replay ID")
        links.append({"fixture_id": expected["id"], "http_request_id": sent["request_id"],
                      "public_response_id": sent["response_id"], "runtime_request_id": actual["request_id"],
                      "input_token_hash": actual["input_tokens_sha256"],
                      "frontend_log_bridge": bridge,
                      "join": "observer header via stock frontend log, verified by exact token array" if bridge else
                              "exact token-array equality and serialized request order in an isolated trial"})
    identity = identity_module.BlockIdentity()
    real_sets = [identity.blocks(row["payload"]["prompt"]) for row in rows if row["kind"] == "real"]
    real = set().union(*real_sets)
    warm = set().union(*(identity.blocks(row["payload"]["prompt"]) for row in rows if row["kind"] == "warmup"))
    real_rows = [(row, sent) for row, sent in zip(rows, requests) if row["kind"] == "real"]
    ledger = collector.PublishedBlocks()
    snapshots = []
    reset_sequence = trial["clear_event"]["sequence"]
    capture = events(trial_dir / "kv-frames.jsonl")
    if not capture or capture[0]["sequence"] != reset_sequence:
        raise ValueError("Trial event slice must begin with the verified clear event")
    if trial["clear_event"].get("payload_sha256") is not None and capture[0]["payload_sha256"] != trial["clear_event"]["payload_sha256"]:
        raise ValueError("Opening clear event differs from its reset proof")
    if not any(event.get("type") == "AllBlocksCleared" for event in capture[0]["batch"][1]):
        raise ValueError("Opening event is not a cache clear")
    if any(event.get("type") == "AllBlocksCleared" for row in capture[1:-1] for event in row["batch"][1]):
        raise ValueError("Unexpected cache clear inside the replay interval")
    closing = capture[-1]
    if (closing["topic"] != config["topic"] or closing["sequence"] != trial["closing_clear"]["sequence"]
            or closing["payload_sha256"] != trial["closing_clear"]["payload_sha256"]
            or len(closing["batch"][1]) != 1 or closing["batch"][1][0].get("type") != "AllBlocksCleared"):
        raise ValueError("Trial event slice lacks its closing clear barrier")
    for event in capture[:-1]:
        if event["topic"] != config["topic"]:
            raise ValueError("KV event topic differs from collector configuration")
        state = ledger.accept(event["sequence"], event["payload_sha256"], event["batch"])
        if state["duplicate"]:
            continue
        stage = len(real_rows) - 1
        if timed:
            stages = [i for i, (_, sent) in enumerate(real_rows) if sent["started_unix"] <= event["batch"][0]]
            if not stages:
                continue
            stage = stages[-1]
        tokens = real_rows[stage][0]["payload"]["prompt"]
        seen_real = set().union(*real_sets[:stage + 1]) if timed else real
        seen_warm = set().union(*(identity.blocks(row["payload"]["prompt"]) for row, sent in zip(rows, requests)
                                  if row["kind"] == "warmup" and (not timed or sent["started_unix"] <= event["batch"][0])))
        snapshot = classify(ledger, identity, seen_real, seen_warm, real_sets[stage], len(tokens))
        if not timed:
            snapshot["warmup_only_to_current_full_blocks"] = snapshot["warmup_only_to_current_tokens"] = None
        snapshot.update(sequence=event["sequence"], event_timestamp_unix=event["batch"][0],
                        current_fixture_id=real_rows[stage][0]["id"] if timed else None)
        snapshots.append(snapshot)
    final = classify(ledger, identity, real, warm, real_sets[-1], len(real_rows[-1][0]["payload"]["prompt"]))
    ledger.accept(closing["sequence"], closing["payload_sha256"], closing["batch"])
    attribution = bool(final["attributable"]) and not ledger.issues
    if not attribution:
        final["warmup_only_to_current_full_blocks"] = final["warmup_only_to_current_tokens"] = None
    result = {"session": run["session"], "condition": run["condition"], "repeat": run["repeat"],
              "scope": "published full input-prefix entries; active partial allocations are absent",
              "input_linkage": links, "attributable": attribution, "sequence_issues": ledger.issues,
              "wire_selection": {"first_sent_unix": first_sent, "last_ended_unix": last_ended,
                                 "clock": "same-pod x-frontend-send-ts-ns" if timed else None},
              "per_snapshot_context_timing_available": timed, "snapshots": snapshots, "terminal": final,
              "physical_kv_bytes_measured": False, "scheduled_prefill_tokens": None,
              "request_metrics": [{"fixture_id": row["id"], "kind": row["kind"],
                  "prompt_tokens": sent["prompt_tokens"], "cached_tokens": sent["cached_tokens"],
                  "uncached_prompt_tokens": sent["prompt_tokens"] - sent["cached_tokens"],
                  "local_compute_prefill_tokens": computed_prefill(sent, trial_dir / "replay"),
                  "client_first_visible_text_seconds": sent["ttft_seconds"],
                  "completion_seconds": sent["completion_seconds"]} for row, sent in zip(rows, requests)],
              "limits": ["One-token builder replay omits original assistant decode KV.",
                         "Published prefix entries cannot establish physical cache allocation.",
                         "A prompt-minus-cached count does not measure scheduled prefill work.",
                         "KV event publication timestamps stage snapshots; they do not identify kernel execution."],
              "evidence_sha256": {str(p.relative_to(trial_dir)): sha(p.read_bytes()) for p in
                    (trial_dir / "trial.json", trial_dir / "kv-config.json", trial_dir / "kv-frames.jsonl",
                     trial_dir / "replay/run.json", trial_dir / "replay/requests.jsonl")},
              "wire_sha256": sha(wire_path.read_bytes())}
    computed = [row["local_compute_prefill_tokens"]["value"] for row in result["request_metrics"]]
    result["scheduled_prefill_tokens"] = sum(computed) if all(value is not None for value in computed) else None
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trial", type=Path)
    parser.add_argument("--wire-decoded", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(report(args.trial, args.wire_decoded), indent=2) + "\n")
    print(str(args.output))
