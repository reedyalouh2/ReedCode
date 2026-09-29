"""Freeze or verify the pinned source behind the cache-reset procedure."""

import argparse
import gzip
import io
import json
from pathlib import Path
import subprocess
import tarfile

from prepare import FINDING, ROOT, json_bytes, sha


PINS = {
    "main": "f5d3353e2167bb0f0d729085eb5bc9183bf4b222",
    "release": "b83b1d9304ebfc624709ac46db32b1b6f1ff1615",
}
PATHS = [
    "components/src/dynamo/vllm/worker_factory.py",
    "components/src/dynamo/vllm/handlers.py",
    "lib/bindings/python/src/dynamo/runtime/__init__.py",
    "lib/bindings/python/src/dynamo/_core.pyi",
    "lib/kv-router/src/zmq_wire/convert.rs",
    "lib/kv-router/src/indexer/radix_tree.rs",
]
VLLM_PATHS = {
    "vllm/v1/core/block_pool.py",
    "vllm/v1/core/sched/scheduler.py",
    "vllm/distributed/kv_events.py",
}


def capture(checkouts):
    records, data = [], {}
    for label, revision in PINS.items():
        for path in PATHS:
            raw = subprocess.check_output(["git", "-C", str(checkouts[label]), "show", revision + ":" + path])
            member = label + "/" + path
            data[member] = raw
            records.append({"member": member, "revision": revision, "path": path,
                            "sha256": sha(raw), "bytes": len(raw),
                            "url": f"https://github.com/ai-dynamo/dynamo/blob/{revision}/{path}"})
    old = json.loads((FINDING / "router/manifest.json").read_text())
    archive = FINDING / "router" / old["storage"]["archive"]
    if sha(archive.read_bytes()) != old["storage"]["sha256"]:
        raise ValueError("Router source archive changed")
    with tarfile.open(archive, "r:gz") as tar:
        for row in old["files"]:
            if row.get("repository") != "vllm-project/vllm" or row["path"] not in VLLM_PATHS:
                continue
            raw = tar.extractfile(row["archive_member"]).read()
            if sha(raw) != row["sha256"]:
                raise ValueError("Pinned vLLM source changed")
            member = "vllm/" + row["path"]
            data[member] = raw
            records.append({"member": member, "revision": row["revision"], "path": row["path"],
                            "sha256": sha(raw), "bytes": len(raw), "url": row["url"]})
    if len(data) != 15:
        raise ValueError("Missing reset source files")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, raw in sorted(data.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(raw), 0, 0o644
            tar.addfile(info, io.BytesIO(raw))
    packed = gzip.compress(buffer.getvalue(), mtime=0)
    (ROOT / "reset-source.tar.gz").write_bytes(packed)
    (ROOT / "reset-source.json").write_bytes(json_bytes({
        "archive": "reset-source.tar.gz", "sha256": sha(packed), "sources": records,
        "status": "Source inspection only; no worker or router reset executed",
    }))


def verify():
    manifest = json.loads((ROOT / "reset-source.json").read_text())
    archive = ROOT / manifest["archive"]
    if sha(archive.read_bytes()) != manifest["sha256"]:
        raise ValueError("Reset source archive changed")
    with tarfile.open(archive, "r:gz") as tar:
        if set(tar.getnames()) != {r["member"] for r in manifest["sources"]}:
            raise ValueError("Reset source inventory changed")
        for row in manifest["sources"]:
            raw = tar.extractfile(row["member"]).read()
            if len(raw) != row["bytes"] or sha(raw) != row["sha256"]:
                raise ValueError("Reset source hash mismatch: " + row["member"])
    return {"source_files_verified": len(manifest["sources"]), "server_requests_made": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--main-checkout", type=Path, default=Path("/tmp/reedcode-dynamo-prefill-fix"))
    parser.add_argument("--release-checkout", type=Path, default=Path("/tmp/reedcode-dynamo-v1.5.0"))
    args = parser.parse_args()
    if args.capture:
        capture({"main": args.main_checkout, "release": args.release_checkout})
    print(json.dumps(verify(), indent=2))
