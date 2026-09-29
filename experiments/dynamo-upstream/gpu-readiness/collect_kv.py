"""Save vLLM's original ZMQ frames and track published cacheable GPU blocks."""

import argparse
import base64
import hashlib
import json
import math
from pathlib import Path
import signal
import socket as network_socket
import time


def hash_key(value):
    if isinstance(value, bytes):
        return "bytes:" + value.hex()
    if type(value) is int and value >= 0:
        return "int:" + str(value)
    raise ValueError("Unsupported external block hash")


class PublishedBlocks:
    def __init__(self):
        self.last_sequence = None
        self.last_payload_hash = None
        self.complete_since_clear = False
        self.blocks = {}
        self.history = {}
        self.issues = []

    def accept(self, sequence, payload_hash, batch):
        issue = None
        if sequence == self.last_sequence and payload_hash == self.last_payload_hash:
            return {"duplicate": True, "complete_since_clear": self.complete_since_clear,
                    "published_gpu_blocks": len(self.blocks)}
        if self.last_sequence is not None and sequence != self.last_sequence + 1:
            self.complete_since_clear = False
            issue = "sequence_gap_or_reset"
        self.last_sequence, self.last_payload_hash = sequence, payload_hash
        # vLLM's EventBatch is an array-like msgspec Struct.
        if not isinstance(batch, list) or len(batch) not in (2, 3):
            raise ValueError("Expected a vLLM KVEventBatch array")
        timestamp, events = batch[:2]
        rank = batch[2] if len(batch) == 3 else None
        if (type(timestamp) not in (int, float) or not math.isfinite(timestamp)
                or not isinstance(events, list) or rank not in (None, 0)):
            raise ValueError("Invalid batch or unexpected data-parallel rank")
        stored = removed = cleared = 0
        for event in events:
            if not isinstance(event, dict):
                raise ValueError("Expected a tagged KV event mapping")
            kind = event.get("type")
            if kind == "AllBlocksCleared":
                self.blocks.clear()
                self.history.clear()
                self.complete_since_clear = True
                cleared += 1
                continue
            if kind not in {"BlockStored", "BlockRemoved"}:
                self.complete_since_clear = False
                raise ValueError("Unknown KV event type")
            if event.get("medium") != "GPU" or event.get("group_idx") not in (None, 0):
                self.complete_since_clear = False
                raise ValueError("This collector expects the single GPU attention group")
            hashes = [hash_key(value) for value in event["block_hashes"]]
            if kind == "BlockRemoved":
                for key in hashes:
                    removed += key in self.blocks
                    self.blocks.pop(key, None)
                continue
            if event.get("block_size") != 16 or len(event["token_ids"]) != 16 * len(hashes):
                self.complete_since_clear = False
                raise ValueError("Stored event does not describe complete 16-token blocks")
            if event.get("lora_id") is not None or event.get("lora_name") is not None:
                self.complete_since_clear = False
                raise ValueError("LoRA block identity is outside this study")
            if any(key not in (None, []) for key in (event.get("extra_keys") or [])):
                self.complete_since_clear = False
                raise ValueError("Extra cache keys are outside this text-only study")
            parent = event["parent_block_hash"]
            parent = hash_key(parent) if parent is not None else None
            for index, key in enumerate(hashes):
                record = {"parent": parent, "token_ids": event["token_ids"][16*index:16*(index+1)]}
                if key in self.history and self.history[key] != record:
                    self.complete_since_clear = False
                    raise ValueError("Block hash has inconsistent ancestry or tokens")
                stored += key not in self.blocks
                self.blocks[key] = self.history[key] = record
                parent = key
        if issue:
            self.issues.append({"sequence": sequence, "issue": issue})
        return {"duplicate": False, "complete_since_clear": self.complete_since_clear,
                "published_gpu_blocks": len(self.blocks), "newly_published_blocks": stored,
                "removed_present_blocks": removed, "clear_events": cleared, "issue": issue}


def collect(endpoint, topic, output, worker_epoch, max_seconds):
    import msgspec
    import zmq

    output.mkdir(parents=True, exist_ok=False)
    (output / "config.json").write_text(json.dumps({
        "endpoint": endpoint, "topic": topic, "operator_supplied_worker_epoch": worker_epoch,
        "hostname": network_socket.gethostname(), "capture_started_unix": time.time(),
        "maximum_seconds": max_seconds, "physical_bytes_per_block": None,
        "scope": "published cacheable GPU blocks; partial allocations are absent"}, indent=2) + "\n")
    context = zmq.Context()
    socket = context.socket(zmq.SUB)
    socket.setsockopt(zmq.SUBSCRIBE, topic.encode())
    socket.setsockopt(zmq.RCVHWM, 100000)
    socket.setsockopt(zmq.LINGER, 0)
    socket.connect(endpoint)
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    started = time.monotonic()
    ledger = PublishedBlocks()
    failures = 0
    try:
        with (output / "frames.jsonl").open("x", buffering=1) as raw:
            while not stopping and time.monotonic() - started < max_seconds:
                if not socket.poll(250):
                    continue
                frames = socket.recv_multipart()
                record = {"received_unix": time.time(), "offset_seconds": time.monotonic() - started,
                          "frames_base64": [base64.b64encode(frame).decode() for frame in frames]}
                try:
                    if len(frames) != 3 or frames[0] != topic.encode() or len(frames[1]) != 8:
                        raise ValueError("Unexpected publisher frame layout")
                    sequence = int.from_bytes(frames[1], "big")
                    record["sequence"] = sequence
                    record["payload_sha256"] = hashlib.sha256(frames[2]).hexdigest()
                    record["state"] = ledger.accept(sequence, record["payload_sha256"],
                                                    msgspec.msgpack.decode(frames[2]))
                except (ValueError, TypeError, KeyError, IndexError, msgspec.DecodeError) as error:
                    failures += 1
                    ledger.complete_since_clear = False
                    record["decode_error_type"] = type(error).__name__
                raw.write(json.dumps(record) + "\n")
    finally:
        socket.close()
        context.term()
        (output / "summary.json").write_text(json.dumps({
            "last_sequence": ledger.last_sequence, "decode_failures": failures,
            "complete_since_clear": ledger.complete_since_clear,
            "sequence_issues": ledger.issues, "published_gpu_blocks": len(ledger.blocks),
            "resident_block_records": ledger.blocks, "historical_block_records": ledger.history,
            "physical_kv_bytes_measured": False}, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="tcp://127.0.0.1:5557")
    parser.add_argument("--topic", default="kv-events")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker-epoch", required=True)
    parser.add_argument("--max-seconds", type=float, default=8400)
    args = parser.parse_args()
    if not args.endpoint.startswith("tcp://127.0.0.1:") or not 0 < args.max_seconds <= 9000:
        parser.error("Use the local publisher and a positive duration up to 150 minutes")
    collect(args.endpoint, args.topic, args.output, args.worker_epoch, args.max_seconds)
