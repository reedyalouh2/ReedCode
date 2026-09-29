"""Package the frozen scripts, public fixtures and matched frontend wheels."""

import argparse
import hashlib
import json
from pathlib import Path
import tarfile


def package(repository, artifacts, output):
    repository, artifacts = repository.resolve(), artifacts.resolve()
    if output.exists():
        raise ValueError("Choose a new bundle path")
    manifest = json.loads((artifacts / "artifact-manifest.json").read_text())
    if set(manifest["wheels"]) != {"stock", "fixed", "common"}:
        raise ValueError("Matched frontend wheels are incomplete")
    files = {}
    def add(path, name):
        if not path.is_file() or path.is_symlink():
            raise ValueError("Bundle entry must be a regular file: " + str(path))
        files[name] = path
    add(artifacts / "artifact-manifest.json", "wheels/artifact-manifest.json")
    for row in manifest["wheels"].values():
        path = (artifacts / row["path"]).resolve()
        if artifacts not in path.parents or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("Wheel manifest verification failed")
        add(path, "wheels/" + row["path"])
    for name in ("metrics_identity.py", "server_metrics.py", "dynamo_metrics.py", "dynamo_support.py"):
        add(repository / name, "repo/" + name)
    folders = ("gpu-readiness", "001-speculative-prefill/gpu-readiness",
               "001-speculative-prefill/linux-readiness", "parity-run-1/wire-capture")
    base = repository / "experiments/dynamo-upstream"
    for folder in folders:
        for path in sorted((base / folder).glob("*")):
            if path.suffix in {".py", ".sh"} or path.name == "hooks.template.json":
                add(path, "repo/" + str(path.relative_to(repository)))
    for path in sorted((base / "001-speculative-prefill/fix").glob("*.patch")):
        add(path, "repo/" + str(path.relative_to(repository)))
    identity = base / "001-speculative-prefill/kv-footprint/reproduce.py"
    add(identity, "repo/" + str(identity.relative_to(repository)))
    probe = base / "001-speculative-prefill/stock_probe.py"
    add(probe, "repo/" + str(probe.relative_to(repository)))
    parity_reset = base / "parity-run-1/readiness/reset_live.py"
    add(parity_reset, "repo/" + str(parity_reset.relative_to(repository)))
    for path in sorted((base / "001-speculative-prefill/gpu-readiness/fixtures").rglob("*")):
        if path.is_file():
            add(path, "repo/" + str(path.relative_to(repository)))
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()}
    with tarfile.open(output, "w:gz") as archive:
        for name, path in files.items():
            archive.add(path, arcname=name, recursive=False)
    record = {"files": hashes, "archive_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
              "entries": len(files), "contains_model_weights": False,
              "contains_api_or_ssh_credentials": False}
    output.with_suffix(output.suffix + ".manifest.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({"archive": str(output), "entries": len(files), "sha256": record["archive_sha256"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    package(args.repository, args.artifacts, args.output)
