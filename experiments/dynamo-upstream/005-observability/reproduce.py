"""Check which operator questions the saved deployment records can answer."""

import argparse
import hashlib
import json
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT.parents[1] / "dynamo-20260928/raw-records.tar.gz"
ARCHIVE_SHA = "4a4650d97c06173e817e21657a651daa7d9d1be20441db92869fb42a584a1cb1"
INTERNAL_IDS = {
    "text": "24f93c56-30d4-411a-9eff-d0b3699652ed",
    "tool": "7e0604ef-aead-454d-bf62-3172907a2c1f",
}


def reproduce():
    digest = hashlib.sha256(ARCHIVE.read_bytes()).hexdigest()
    if digest != ARCHIVE_SHA:
        raise ValueError("The source archive has changed")
    with tarfile.open(ARCHIVE) as archive:
        checksums = json.load(archive.extractfile("sha256.json"))
        records = {}
        for name, expected in checksums.items():
            data = archive.extractfile(name).read()
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError(f"Archive member changed: {name}")
            records[name] = data

    metrics = []
    for name, data in sorted(records.items()):
        if name.endswith(".prom"):
            sample_names = {line.split("{", 1)[0].split()[0]
                            for line in data.decode().splitlines()
                            if line and not line.startswith("#")}
            metrics.append({"member": name, "sha256": checksums[name],
                            "process_start_present": "process_start_time_seconds" in sample_names})
    epochs = []
    for name, data in sorted(records.items()):
        if name.startswith("replay/") and name.endswith("/epoch.json"):
            record = json.loads(data)
            metric = record["metrics"]
            epochs.append({"member": name, "sha256": checksums[name],
                           **{key: metric[key] for key in (
                               "valid_boundaries", "restart_observable", "idle_before", "drained", "errors")}})

    frontend = records["server/frontend.log"].decode().splitlines()
    traces = records["server/frontend-trace.jsonl"].decode().splitlines()
    backend = records["server/backend.log"].decode().splitlines()
    preparations = []
    for case, ident in INTERNAL_IDS.items():
        preparations.append({"case": case, "internal_request_id": ident,
                             "frontend_log_lines": [i for i, line in enumerate(frontend, 1) if ident in line],
                             "frontend_trace_lines": [i for i, line in enumerate(traces, 1) if ident in line],
                             "backend_log_lines": [i for i, line in enumerate(backend, 1) if ident in line]})
    if not metrics or not epochs or any(not p["backend_log_lines"] for p in preparations):
        raise ValueError("Expected deployment records are missing")
    return {
        "archive_sha256": digest, "verified_archive_members": len(records),
        "deployment": json.loads(records["setup/deployment.json"]),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "prometheus_snapshots": metrics, "replay_epochs": epochs,
        "internal_preparations": preparations,
        "limits": [
            "This checks the recorded deployment; it does not test current main.",
            "An absent process-start metric does not prove a restart occurred.",
            "No frontend HTTP trace entry is expected for an internal request; the missing answer is its parent/session join.",
            "Log adjacency establishes attribution only in these isolated checks.",
            "Cache occupancy samples cannot identify which prepared blocks were later reused.",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    text = json.dumps(reproduce(), indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    else:
        print(text, end="")
