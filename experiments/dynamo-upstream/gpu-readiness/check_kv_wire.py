"""Check the collector against vLLM 0.28 event classes and a local ZMQ stream."""

import argparse
import ast
import hashlib
import json
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import time
from typing import Any

import msgspec
import zmq


def check(archive, output):
    output.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive) as source:
        raw = source.extractfile("vllm/vllm/distributed/kv_events.py").read()
    names = {"EventBatch", "KVCacheEvent", "BlockStored", "BlockRemoved",
             "AllBlocksCleared", "KVEventBatch"}
    definitions = ast.Module(body=[node for node in ast.parse(raw).body
                                  if isinstance(node, ast.ClassDef) and node.name in names],
                             type_ignores=[])
    namespace = {"msgspec": msgspec, "Any": Any, "ExternalBlockHash": int | bytes}
    exec(compile(definitions, "pinned_vllm_kv_events.py", "exec"), namespace)
    BlockStored, BlockRemoved, Cleared, Batch = [namespace[name] for name in
        ("BlockStored", "BlockRemoved", "AllBlocksCleared", "KVEventBatch")]
    events = [Batch(ts=100.0, events=[Cleared()], data_parallel_rank=0),
              Batch(ts=101.0, events=[BlockStored(
                  block_hashes=[1, 2], parent_block_hash=None, token_ids=list(range(32)),
                  block_size=16, lora_id=None, medium="GPU", lora_name=None,
                  extra_keys=[None, None], group_idx=0)], data_parallel_rank=0),
              Batch(ts=102.0, events=[BlockRemoved(block_hashes=[1], medium="GPU",
                                                  group_idx=0)], data_parallel_rank=0)]
    payloads = [msgspec.msgpack.encode(event) for event in events]
    assert all(isinstance(msgspec.msgpack.decode(payload), list) for payload in payloads)
    context = zmq.Context()
    publisher = context.socket(zmq.XPUB)
    publisher.setsockopt(zmq.LINGER, 0)
    port = publisher.bind_to_random_port("tcp://127.0.0.1")
    command = [sys.executable, str(Path(__file__).with_name("collect_kv.py")),
               "--endpoint", f"tcp://127.0.0.1:{port}", "--output", str(output / "capture"),
               "--worker-epoch", "cpu-wire-fixture", "--max-seconds", "10"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert publisher.poll(5000), "Collector did not subscribe"
        assert publisher.recv() == b"\x01kv-events"
        for sequence, payload in enumerate(payloads):
            publisher.send_multipart([b"kv-events", sequence.to_bytes(8, "big"), payload])
        deadline = time.monotonic() + 5
        frames_path = output / "capture" / "frames.jsonl"
        while time.monotonic() < deadline:
            if frames_path.exists() and len(frames_path.read_text().splitlines()) == 3:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("Collector did not save all three frames")
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=3)
        assert process.returncode == 0, stderr.decode()
        frames = [json.loads(line) for line in frames_path.read_text().splitlines()]
        summary = json.loads((output / "capture" / "summary.json").read_text())
        assert [record["sequence"] for record in frames] == [0, 1, 2]
        assert all("decode_error_type" not in record for record in frames)
        assert summary["complete_since_clear"] and summary["decode_failures"] == 0
        assert summary["published_gpu_blocks"] == 1
        assert summary["resident_block_records"]["int:2"]["parent"] == "int:1"
        assert len(summary["historical_block_records"]) == 2
        result = {"passed": True, "gpu_used": False, "model_inference": False,
                  "vllm_source_sha256": hashlib.sha256(raw).hexdigest(),
                  "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                  "msgspec_version": msgspec.__version__, "pyzmq_version": zmq.__version__,
                  "batches": 3, "event_classes": "AST-extracted from pinned vLLM source",
                  "transport": "local XPUB/SUB with vLLM multipart layout",
                  "scope": "encoding and collection only; no engine or physical allocation"}
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
        publisher.close()
        context.term()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    check(args.archive, args.output)
