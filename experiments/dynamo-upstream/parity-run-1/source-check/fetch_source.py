#!/usr/bin/env python3
"""Save the stock image revision's public endpoint and capture sources."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import urllib.error
import urllib.request


REVISION = "32b8b2f8c63fa3531c34b64c1cf2cbe39a6f9653"
TAG_REVISION = "b83b1d9304ebfc624709ac46db32b1b6f1ff1615"
FILES = [
    "components/src/dynamo/frontend/frontend_args.py",
    "components/src/dynamo/frontend/main.py",
    "lib/llm/src/http/service/service_v2.rs",
    "lib/llm/src/http/service/anthropic.rs",
    "lib/llm/src/protocols/anthropic/types.rs",
    "lib/llm/src/protocols/anthropic/stream_converter.rs",
    "lib/llm/src/preprocessor.rs",
    "lib/llm/src/protocols/common/preprocessor.rs",
    "lib/llm/src/request_trace/payload.rs",
    "lib/llm/src/request_trace/types.rs",
    "lib/runtime/src/pipeline/network/ingress/push_handler.rs",
    "docs/fern/pages/reference/observability/logging.mdx",
]


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def fetch(url, destination):
    request = urllib.request.Request(url, headers={"User-Agent": "ReedCode-source-check"})
    with urllib.request.urlopen(request, timeout=40) as response:
        data = response.read()
        status = response.status
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return {"url": url, "http_status": status, "sha256": sha256(data), "bytes": len(data)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag-checkout", type=Path, required=True)
    args = parser.parse_args()
    checkout_revision = subprocess.check_output(
        ["git", "-C", str(args.tag_checkout), "rev-parse", "HEAD"], text=True
    ).strip()
    if checkout_revision != TAG_REVISION:
        raise RuntimeError(f"Expected tag checkout {TAG_REVISION}, got {checkout_revision}")
    root = Path(__file__).resolve().parent
    manifest = {
        "retrieved_utc": datetime.now(timezone.utc).isoformat(),
        "image_reported_revision": REVISION,
        "comparison_tag_revision": TAG_REVISION,
        "sources": [],
    }
    metadata_path = root / "commit.json"
    manifest["commit_metadata"] = fetch(
        f"https://api.github.com/repos/ai-dynamo/dynamo/commits/{REVISION}", metadata_path
    )
    metadata = json.loads(metadata_path.read_bytes())
    if metadata["sha"] != REVISION:
        raise RuntimeError("GitHub resolved an unexpected revision")

    def fetch_one(relative):
        destination = root / "image" / relative
        url = f"https://raw.githubusercontent.com/ai-dynamo/dynamo/{REVISION}/{relative}"
        entry = {"path": str(destination.relative_to(root)), "repository_path": relative}
        try:
            entry.update(fetch(url, destination))
        except urllib.error.HTTPError as error:
            entry.update({"url": url, "http_status": error.code, "error": str(error)})
        tag_data = subprocess.check_output(
            ["git", "-C", str(args.tag_checkout), "show", f"{TAG_REVISION}:{relative}"]
        )
        tag_hash = sha256(tag_data)
        entry["comparison_tag_sha256"] = tag_hash
        entry["identical_to_comparison_tag"] = entry.get("sha256") == tag_hash
        return entry

    with ThreadPoolExecutor(max_workers=4) as pool:
        manifest["sources"] = list(pool.map(fetch_one, FILES))
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    for entry in manifest["sources"]:
        print(entry["http_status"], entry["repository_path"],
              "same" if entry.get("identical_to_comparison_tag") else "different/missing")


if __name__ == "__main__":
    main()
