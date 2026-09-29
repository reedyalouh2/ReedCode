#!/usr/bin/env python3
"""Build report mappings from recorder headers and decoded stock frontend logs."""

import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def relative(path, base):
    return os.path.relpath(path.resolve(), base.resolve())


def observer_id(meta):
    original = meta.get("original_x_request_ids", [])
    injected = meta.get("observer_x_request_id")
    if len(original) > 1 or (original and injected):
        raise ValueError("Ambiguous observer header in recorder metadata")
    return original[0] if original else injected


def wire_index(wire, wire_path):
    linked, internal = {}, set()
    sources = wire.get("frontend_log_linkage", {}).get("files", {})
    for index, row in enumerate(wire["requests"]):
        if row.get("kind", "model") != "model":
            continue
        request_id = row["request_id"]
        if request_id in internal:
            raise ValueError("Duplicate wire request ID")
        internal.add(request_id)
        link = row.get("http_link")
        if not link:
            continue
        header = link["http_request_id"]
        if header in linked or link["runtime_request_id"] != request_id:
            raise ValueError("Ambiguous HTTP-to-wire link")
        raw_log = Path(link["frontend_log"])
        if not raw_log.is_absolute():
            raw_log = wire_path.parent / raw_log
        raw = raw_log.read_bytes()
        if sha(raw) != sources.get(link["frontend_log"]):
            raise ValueError("Frontend log hash mismatch")
        line = json.loads(raw.splitlines()[link["line"] - 1])
        if (line != link["row"] or line.get("message") != "request received"
                or line.get("request_id") != request_id or line.get("x_request_id") != header):
            raise ValueError("Frontend log row does not prove the claimed link")
        linked[header] = (index, row)
    return linked


def backend_usage(row, reference):
    records = []
    for index, response in enumerate(row.get("responses", [])):
        payload = ((response.get("wrapper") or {}).get("data") or {}).get("data")
        usage = payload.get("completion_usage") if isinstance(payload, dict) else None
        if usage is None:
            continue
        total = usage.get("prompt_tokens")
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        if type(total) is int and total >= 0 and type(cached) is int and 0 <= cached <= total:
            records.append((total, cached, index))
    if not records:
        return None
    if len({(total, cached) for total, cached, _ in records}) != 1:
        raise ValueError("Backend usage changes its input/cache count within one request")
    total, cached, index = records[-1]
    if total != len(row["input_token_ids"]):
        raise ValueError("Backend usage differs from captured input IDs")
    return {"input_tokens": total, "cached_tokens": cached,
            "evidence": [reference + f"/responses/{index}/wrapper/data/data/completion_usage"]}


def build_mapping(wire_path, session_paths, worker_id, epoch, identity_evidence, output,
                  labels=None, main_reviewed=False):
    if not main_reviewed:
        raise ValueError("Review inference bodies for auxiliary requests before assigning main conversation")
    labels = labels or {}
    wire_path, output = wire_path.resolve(), output.resolve()
    raw = wire_path.read_bytes()
    wire, digest = json.loads(raw), sha(raw)
    linked = wire_index(wire, wire_path)
    used, observed, label_uses = set(), set(), set()
    identity_raw = identity_evidence.read_bytes()
    identity = {"path": relative(identity_evidence, output.parent), "sha256": sha(identity_raw),
                "worker_id": worker_id, "epoch": epoch}
    mapping = {"block_size": 16, "sessions": [], "join": {
        "wire": relative(wire_path, output.parent), "wire_sha256": digest,
        "identity": identity, "main_conversation_reviewed": True,
        "unlinked_capture_ids": [], "non_inference_captures": [], "controller_evidence": []}}
    for client, directory in session_paths.items():
        directory = directory.resolve()
        result_path = directory / "result.json"
        result_raw = result_path.read_bytes()
        result = json.loads(result_raw)
        if result.get("client") != client or not result.get("session_id"):
            raise ValueError("Controller client/session identity missing")
        session = {"id": client + "-" + result["session_id"],
                   "protocol": "anthropic" if client == "claude" else "responses", "requests": []}
        mapping["join"]["controller_evidence"].append({"path": relative(result_path, output.parent),
            "sha256": sha(result_raw), "stub_only": result.get("stub_only"), "smoke": result.get("smoke")})
        captures = [(path, json.loads(path.read_text())) for path in directory.glob("capture/*/metadata.json")]
        captures.sort(key=lambda pair: pair[1]["started_monotonic_ns"])
        for path, meta in captures:
            capture_id = client + "-" + path.parent.name
            if not meta.get("inference"):
                mapping["join"]["non_inference_captures"].append(capture_id)
                continue
            expected_route = "/v1/messages" if client == "claude" else "/v1/responses"
            if urlsplit(meta["path"]).path.rstrip("/") != expected_route:
                raise ValueError("Unreviewed auxiliary inference route: " + capture_id)
            stamp = meta["started_monotonic_ns"]
            turns = [i + 1 for i, turn in enumerate(result["turns"])
                     if turn["started_monotonic_ns"] <= stamp <= turn["ended_monotonic_ns"]]
            if len(turns) != 1:
                raise ValueError("Capture does not belong to exactly one controller turn: " + capture_id)
            label = labels.get(capture_id, {})
            if label:
                if not label.get("evidence") or not label.get("conversation_id"):
                    raise ValueError("Request classification override needs conversation and evidence")
                label_uses.add(capture_id)
            entry = {"id": capture_id, "user_turn": turns[0],
                     "conversation_id": label.get("conversation_id", "main"),
                     "capture": relative(path.parent, output.parent), "link_evidence": []}
            if label.get("auxiliary"):
                entry["auxiliary"] = True
            if label:
                entry["classification_evidence"] = label["evidence"]
            header = observer_id(meta)
            if header:
                if header in observed:
                    raise ValueError("Observer header reused across recorder captures")
                observed.add(header)
            match = linked.get(header)
            if match is None:
                mapping["join"]["unlinked_capture_ids"].append(capture_id)
            else:
                index, row = match
                if row.get("complete") is not True:
                    raise ValueError("Linked backend stream is incomplete")
                if row["request_id"] in used:
                    raise ValueError("Wire request reused by multiple captures")
                used.add(row["request_id"])
                reference = relative(wire_path, output.parent) + f"#/requests/{index}"
                entry["backend"] = {"path": relative(wire_path, output.parent), "sha256": digest,
                    "token_ids_pointer": f"/requests/{index}/input_token_ids",
                    "request_id": row["request_id"], "worker_id": worker_id, "epoch": epoch}
                entry["link_evidence"] = [relative(path, output.parent) + "#observer_x_request_id/original_x_request_ids",
                    reference + "/http_link", relative(result_path, output.parent) + f"#/turns/{turns[0] - 1}",
                    identity["path"] + " (sha256 " + identity["sha256"] + ")"]
                usage = backend_usage(row, reference)
                if usage is not None:
                    entry["backend_usage"] = usage
            session["requests"].append(entry)
        mapping["sessions"].append(session)
    if set(labels) != label_uses:
        raise ValueError("Classification labels contain unknown or empty entries")
    mapping["join"]["unused_wire_model_request_ids"] = [row["request_id"] for row in wire["requests"]
        if row.get("kind", "model") == "model" and row["request_id"] not in used]
    return mapping


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wire", type=Path, required=True)
    parser.add_argument("--claude", type=Path)
    parser.add_argument("--codex", type=Path)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--epoch", required=True)
    parser.add_argument("--identity-evidence", type=Path, required=True)
    parser.add_argument("--labels", type=Path)
    parser.add_argument("--main-conversation-reviewed", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or not (args.claude or args.codex):
        parser.error("Use a new output path and at least one recorded session")
    mapping = build_mapping(args.wire, {k: v for k, v in {"claude": args.claude, "codex": args.codex}.items() if v},
        args.worker_id, args.epoch, args.identity_evidence, args.output,
        json.loads(args.labels.read_text()) if args.labels else None, args.main_conversation_reviewed)
    args.output.write_text(json.dumps(mapping, indent=2) + "\n")
    print(json.dumps({"mapping": str(args.output), "sessions": len(mapping["sessions"]),
        "unlinked_capture_ids": mapping["join"]["unlinked_capture_ids"]}, indent=2))
