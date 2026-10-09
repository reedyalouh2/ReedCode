#!/usr/bin/env python3
"""Apply the pinned routing patches and run their native CPU tests."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import urllib.request

HERE = Path(__file__).resolve().parent


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=("hybrid", "carrier"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--full", action="store_true", help="Also run the full carrier library suite")
    args = parser.parse_args()
    require(sys.version_info >= (3, 12), "Python 3.12+ is required")
    output = args.output.resolve()
    require(not output.exists(), "Choose a new output directory")
    pins = json.loads((HERE / "pins.json").read_text())
    pin = pins[args.case]
    output.mkdir(parents=True)
    archive = args.archive.resolve() if args.archive else output / "source.tar.gz"
    if not args.archive:
        require(not args.offline, "--offline requires --archive")
        with urllib.request.urlopen(pin["archive_url"], timeout=60) as remote, archive.open("xb") as local:
            size = 0
            while chunk := remote.read(1024 * 1024):
                size += len(chunk)
                require(size <= 128 * 1024 * 1024, "Source archive exceeds download limit")
                local.write(chunk)
    require(digest(archive) == pin["archive_sha256"], "Source archive hash mismatch")
    root = "dynamo-" + pin["commit"]
    with tarfile.open(archive) as bundle:
        members = bundle.getmembers()
        require(sum(m.size for m in members) < 1024**3, "Source archive exceeds extraction limit")
        require(all(Path(m.name).parts[0] == root for m in members), "Unexpected archive root")
        bundle.extractall(output, filter="data")
    source = output / root

    def patch(name):
        with (output / (name + ".log")).open("w") as log:
            subprocess.run(
                ["patch", "--batch", "--forward", "-p1", "-i", str(HERE / name)],
                cwd=source, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=60,
            )

    if args.case == "hybrid":
        patch("hybrid.patch")
        manifest = json.loads((HERE / "hybrid-source-hashes.json").read_text())
        require(manifest["base_commit"] == pin["commit"], "Source manifest commit mismatch")
        require(digest(HERE / "hybrid.patch") == manifest["patch_sha256"], "Patch hash mismatch")
        for name, expected in manifest["files"].items():
            require(digest(source / name) == expected, "Patched source mismatch: " + name)
    else:
        patch("carrier-tests.patch")
    if args.prepare_only:
        if args.case == "carrier":
            patch("carrier-fix.patch")
        print("Patch application verified: " + str(source))
        return

    for tool in ("cargo", "rustc"):
        version = subprocess.check_output([tool, "--version"], text=True)
        require(version.startswith(tool + " " + pins["rust"] + " "), "Use " + tool + " " + pins["rust"])
    env = os.environ.copy()
    env.setdefault("CARGO_BUILD_JOBS", "2")
    env["RUSTFLAGS"] = (env.get("RUSTFLAGS", "") + " --cfg tokio_unstable").strip()

    def run(label, test_filter=None):
        command = ["cargo", "test", "--locked"]
        if args.offline:
            command.append("--offline")
        command += ["-p", "dynamo-kv-router", "--features", "standalone-selection", "--lib"]
        if test_filter:
            command += [test_filter, "--", "--nocapture", "--test-threads=1"]
        path = output / (label + ".log")
        print("Running " + label + "; log: " + str(path), flush=True)
        with path.open("w") as log:
            result = subprocess.run(command, cwd=source, env=env, stdout=log, stderr=subprocess.STDOUT)
        return result.returncode, path.read_text()

    if args.case == "carrier":
        code, text = run("original", "carrier_")
        failures = set(re.findall(r"^    (\w+(?:::\w+)+)$", text.rsplit("\nfailures:\n", 1)[-1], re.MULTILINE))
        expected = set(json.loads((HERE / "carrier-results.json").read_text())["gap_reproduction"]["expected_baseline_failures"])
        require(code == 101 and failures == expected, "Original code did not produce the expected six failures; inspect original.log")
        patch("carrier-fix.patch")
        code, _ = run("fixed", "carrier_")
        require(code == 0, "Fixed regressions failed; inspect fixed.log")
    if args.case == "hybrid" or args.full:
        code, _ = run("full-suite")
        require(code == 0, "Full suite failed; inspect full-suite.log")
    print("Tests passed: " + str(output))


if __name__ == "__main__":
    main()
