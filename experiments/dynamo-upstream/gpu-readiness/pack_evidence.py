"""Snapshot study evidence while excluding model weights and Python environments."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import tarfile
import time


def pack(study, output, final=False):
    study, output = study.resolve(), output.resolve()
    if study in output.parents or not study.is_dir():
        raise ValueError("Write the evidence archive outside the existing study directory")
    temporary = output.with_suffix(output.suffix + ".partial")
    manifest = {"captured_unix": time.time(), "final_after_process_stop": final, "files": {}}
    with tarfile.open(temporary, "w:gz") as archive:
        for path in sorted(study.rglob("*")):
            relative = path.relative_to(study)
            if relative.parts[0] in {"hf", "frontend-stock", "frontend-fixed"} or path.is_symlink() or not path.is_file():
                continue
            data = path.read_bytes()
            name = str(relative)
            item = tarfile.TarInfo(name)
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
            manifest["files"][name] = hashlib.sha256(data).hexdigest()
        raw = (json.dumps(manifest, indent=2) + "\n").encode()
        item = tarfile.TarInfo("snapshot-manifest.json")
        item.size = len(raw)
        archive.addfile(item, io.BytesIO(raw))
    temporary.replace(output)
    return {"archive": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "files": len(manifest["files"]), "final": final}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()
    print(json.dumps(pack(args.study, args.output, args.final)))
