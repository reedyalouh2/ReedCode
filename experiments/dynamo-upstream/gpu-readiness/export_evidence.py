"""Copy periodic evidence snapshots to the Mac until the study pod is deleted."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time


def export(args):
    args.output.mkdir(parents=True, exist_ok=True)
    state = json.loads(args.state.read_text())
    shared = [sys.executable, str(Path(__file__).with_name("remote.py")),
              "--state", str(args.state), "--cli", str(args.cli),
              "--key-file", str(args.key_file), "--ssh-key", str(args.ssh_key)]
    packer = "/tmp/reedcode-bundle/repo/experiments/dynamo-upstream/gpu-readiness/pack_evidence.py"
    remote_archive = "/tmp/reedcode-study-evidence.tar.gz"
    with (args.output / "exports.jsonl").open("a", buffering=1) as log:
        while time.time() < state["allocation_started_unix"] + 140 * 60:
            if (args.state.parent / "deleted").exists() or (args.state.parent / "study_done").exists():
                return
            record = {"started_unix": time.time()}
            try:
                result = subprocess.run(shared + ["exec", "--", "python3", packer,
                    "--study", "/tmp/reedcode-study", "--output", remote_archive],
                    capture_output=True, timeout=90)
                record["pack_returncode"] = result.returncode
                if result.returncode == 0:
                    snapshot = json.loads(result.stdout)
                    pending = args.output / "latest.partial"
                    copied = subprocess.run(shared + ["download", remote_archive, str(pending)],
                                            capture_output=True, timeout=90)
                    record["copy_returncode"] = copied.returncode
                    if copied.returncode == 0:
                        checksum = hashlib.sha256(pending.read_bytes()).hexdigest()
                        if checksum != snapshot["sha256"]:
                            raise ValueError("Export checksum differs from the remote snapshot")
                        pending.replace(args.output / "latest.tar.gz")
                        record.update(sha256=checksum, files=snapshot["files"])
            except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
                record["error_type"] = type(error).__name__
            log.write(json.dumps(record) + "\n")
            time.sleep(45)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("state", "cli", "key-file", "ssh-key", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    export(parser.parse_args())
