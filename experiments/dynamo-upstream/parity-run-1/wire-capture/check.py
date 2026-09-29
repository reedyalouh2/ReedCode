"""Reproduce the passive-capture checks using saved CPU evidence only."""

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile

from decode import decode, json_bytes, sha, link_frontend_logs


ROOT = Path(__file__).resolve().parent


def main():
    manifest = json.loads((ROOT / "evidence.json").read_text())
    raw = (ROOT / "evidence.tar.gz").read_bytes()
    if sha(raw) != manifest["archive_sha256"]:
        raise ValueError("Capture archive changed")
    summaries = []
    with tarfile.open(ROOT / "evidence.tar.gz") as archive:
        for name, expected in manifest["files"].items():
            if sha(archive.extractfile(name).read()) != expected:
                raise ValueError("Capture evidence changed: " + name)
        baselines = {}
        for client in ("codex", "claude", "codex-ids", "claude-ids"):
            report = decode(archive.extractfile(client + "/runtime.pcap").read())
            callbacks = [json.loads(archive.extractfile(f"{client}/backend-{i}.json").read()) for i in (1, 2)]
            if [row["request"] for row in report["requests"]] != callbacks:
                raise ValueError("Wire request differs from its received callback")
            if any(row["output_token_ids"] != [151668, 198, 151645] for row in report["requests"]):
                raise ValueError("Wire response differs from the fixed worker output")
            linkage = None
            if client.endswith("-ids"):
                name = client + "/frontend.log"
                linkage = link_frontend_logs(report, [(name, archive.extractfile(name).read())])
                if linkage["matched"] != 2 or linkage["unlinked_model_request_ids"]:
                    raise ValueError("Observer header lacks a stock frontend-to-wire bridge")
                if [r["input_token_ids"] for r in report["requests"]] != baselines[client[:-4]]:
                    raise ValueError("Observer header changed backend input token IDs")
                outcomes = json.loads(archive.extractfile(client + "/outcomes.json").read())
                for index, row in enumerate(report["requests"], 1):
                    if row["http_link"]["http_request_id"] != f"wire-{client[:-4]}-{index}":
                        raise ValueError("Observer header and stock log linkage disagree")
            else:
                baselines[client] = [r["input_token_ids"] for r in report["requests"]]
            stats = json.loads(archive.extractfile(client + "/capture.json").read())
            if stats["packet_counts"]["captured"] == 0 or stats["packet_counts"]["dropped"] != 0:
                raise ValueError("Capture was empty or dropped packets")
            summaries.append({"client": client, "pcap_sha256": report["pcap_sha256"],
                "packets": stats["packet_counts"], "callback_payloads_match": True,
                "frontend_log_linkage": linkage,
                "observer_header_preserves_token_ids": True if client.endswith("-ids") else None,
                "requests": [{"request_id": r["request_id"], "input_tokens": len(r["input_token_ids"]),
                              "input_tokens_sha256": r["input_tokens_sha256"],
                              "returned_token_ids": r["output_token_ids"], "complete": r["complete"],
                              "http_link": r.get("http_link")}
                             for r in report["requests"]]})
    source = json.loads((ROOT / "source.json").read_text())
    if sha((ROOT / "source.tar.gz").read_bytes()) != source["archive_sha256"]:
        raise ValueError("Pinned source archive changed")
    with tarfile.open(ROOT / "source.tar.gz") as archive:
        for row in source["files"]:
            if sha(archive.extractfile(row["path"]).read()) != row["sha256"]:
                raise ValueError("Pinned source changed")
    command = [sys.executable, "-m", "unittest", "discover", "-s", str(ROOT), "-p", "test_decode.py"]
    tests = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (ROOT / "tests.log").write_bytes(tests.stdout)
    if tests.returncode:
        raise RuntimeError("Wire decoder tests failed")
    report = {"checked_at_utc": datetime.now(timezone.utc).isoformat(),
              "status": "Passive CPU capture validated against unchanged stock runtime and mock worker",
              "stock_revision": source["revision"], "model_inference_requests": 0, "gpu_requests": 0,
              "captures": summaries, "tests": {"count": int(re.search(rb"Ran (\d+) tests", tests.stdout)[1]),
              "exit_code": tests.returncode, "command": command},
              "script_sha256": {p.name: sha(p.read_bytes()) for p in sorted(ROOT.glob("*.py"))}}
    (ROOT / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
