# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx==0.28.1", "jinja2==3.1.6", "tokenizers==0.23.2"]
# ///
"""Verify the evidence archive and recompute both cohorts on CPU."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

HERE = Path(__file__).resolve().parent


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def extract(destination):
    archive = HERE / "evidence.tar.gz"
    metadata = json.loads((HERE / "evidence.json").read_text())
    if sha(archive) != metadata["archive_sha256"]:
        raise ValueError("Evidence archive checksum mismatch")
    expected = json.loads((HERE / "evidence-files.json").read_text())
    destination.mkdir(parents=True, exist_ok=True)
    seen = set()
    with tarfile.open(archive, "r:gz") as stream:
        for member in stream:
            name = member.name
            if name not in expected or name in seen or not member.isfile():
                raise ValueError("Unexpected or duplicate archive member: " + name)
            target = destination / name
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError("Archive path escapes the evidence directory")
            record = expected[name]
            if member.size != record["bytes"]:
                raise ValueError("Unexpected evidence size: " + name)
            payload = stream.extractfile(member).read()
            if hashlib.sha256(payload).hexdigest() != record["sha256"]:
                raise ValueError("Evidence checksum mismatch: " + name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            seen.add(name)
    if seen != set(expected):
        raise ValueError("Evidence archive is incomplete")


def portable(value, replacements):
    if isinstance(value, dict):
        return {portable(k, replacements): portable(v, replacements) for k, v in value.items()}
    if isinstance(value, list):
        return [portable(v, replacements) for v in value]
    if isinstance(value, str):
        for old, new in replacements:
            value = value.replace(old, new)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=HERE / ".reproduction")
    parser.add_argument("--test", action="store_true", help="Also run the CPU regression tests")
    args = parser.parse_args()
    output = args.output.resolve()
    evidence, results = output / "evidence", output / "results"
    extract(evidence)
    results.mkdir(parents=True, exist_ok=True)
    engine = evidence / "backend/engine.jsonl"
    source = HERE / "source"
    replacements = [(str(evidence), "evidence"), (str(results), "results"), (str(HERE), ".")]
    for cohort in ("original", "amended"):
        commands = [
            ["analyze.py", "--runs", str(evidence / cohort), "--engine", str(engine),
             "--tokenizer-dir", str(evidence / "tokenizer"), "--output", str(results / (cohort + ".json"))],
            ["overlap.py", "--runs", str(evidence / cohort), "--engine", str(engine),
             "--output", str(results / ("overlap-" + cohort + ".json"))],
            ["credit_audit.py", "--engine", str(engine), "--report", str(results / (cohort + ".json")),
             "--output", str(results / ("credit-audit-" + cohort + ".json"))],
        ]
        for command in commands:
            subprocess.run([sys.executable, str(source / command[0]), *command[1:]], check=True)
            file = Path(command[-1])
            value = portable(json.loads(file.read_text()), replacements)
            file.write_text(json.dumps(value, indent=2) + "\n")
    reports = {cohort: json.loads((results / (cohort + ".json")).read_text()) for cohort in ("original", "amended")}
    totals = {}
    for cohort, report in reports.items():
        trials = report["trials"]
        if not all(trial["attribution_valid"] for trial in trials):
            raise ValueError("Attribution failed in " + cohort)
        totals[cohort] = {
            "trials": len(trials),
            "foreground_requests": sum(len(t["normal_requests"]) for t in trials),
            "additional_cached_tokens": sum(t["preparation_credit_lower_bound_tokens"] for t in trials),
        }
        if any(t["preparation_credit_lower_bound_tokens"] for t in trials if t["condition"] == "off"):
            raise ValueError("Unexpected preparation credit in an off trial")
    expected = {
        "original": {"trials": 3, "foreground_requests": 13, "additional_cached_tokens": 1152},
        "amended": {"trials": 8, "foreground_requests": 91, "additional_cached_tokens": 9024},
    }
    if totals != expected:
        raise ValueError("Reproduced counts differ: " + json.dumps(totals))
    rows = [row for trial in reports["amended"]["trials"] for row in trial["normal_requests"]]
    if max(r["preparation_credit_lower_bound_tokens"] for r in rows) != 5968:
        raise ValueError("Largest boundary differs")
    (results / "summary.json").write_text(json.dumps(totals, indent=2) + "\n")
    subprocess.run([sys.executable, str(source / "request_table.py"), str(results)], check=True)
    hashes = {p.name: sha(p) for p in sorted(results.iterdir())
              if p.is_file() and p.suffix in {".json", ".tsv"} and p.name != "sha256.json"}
    (results / "sha256.json").write_text(json.dumps(hashes, indent=2) + "\n")
    if args.test:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(source)
        environment["REEDCODE_TOKENIZER_DIR"] = str(evidence / "tokenizer")
        subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(HERE / "tests")],
                       env=environment, check=True)
    print(json.dumps(totals, indent=2))
    print("Verified. Reports: " + str(results))


if __name__ == "__main__":
    main()
