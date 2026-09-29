"""Run the packet's CPU checks and save their evidence."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

from prepare import REPO, ROOT, json_bytes, sha
from reset_sources import verify as verify_reset_sources
from verify import verify


def check(stock_binary=None, fixed_binary=None):
    packet = verify(stock_binary=stock_binary, fixed_binary=fixed_binary)
    sources = verify_reset_sources()
    command = [sys.executable, "-m", "unittest", "discover", "-s", str(ROOT), "-p", "test_readiness.py"]
    result = subprocess.run(command, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (ROOT / "test-readiness.log").write_bytes(result.stdout)
    if result.returncode:
        raise RuntimeError("CPU tests failed; see test-readiness.log")
    previews = []
    for session in ("short", "long"):
        for condition in ("off", "stock", "fixed"):
            args = [sys.executable, str(ROOT / "replay.py"), "--session", session, "--condition", condition]
            previews.append(json.loads(subprocess.check_output(args, cwd=REPO)))
    reset_args = [sys.executable, str(ROOT / "reset_worker.py"), "--endpoint", "dynamo.backend.clear_kv_blocks"]
    reset_preview = json.loads(subprocess.check_output(reset_args, cwd=REPO))
    trial_preview = json.loads(subprocess.check_output([sys.executable, str(ROOT / "trial.py"),
                                                       "--session", "long", "--condition", "stock"], cwd=REPO))
    smoke_preview = json.loads(subprocess.check_output([sys.executable, str(ROOT / "live_hint_smoke.py")], cwd=REPO))
    phase_preview = json.loads(subprocess.check_output([sys.executable, str(ROOT / "phase_control.py"),
                                                       "switch", "--phase", "prefill"], cwd=REPO))
    paths = sorted([*ROOT.glob("*.py"), *ROOT.glob("*.md"), ROOT / "fixtures/manifest.json",
                    ROOT / "reset-source.json", ROOT / "reset-source.tar.gz", ROOT / "test-readiness.log"])
    report = {"checked_at_utc": datetime.now(timezone.utc).isoformat(),
              "status": "CPU checks passed; real backend and generated-hint smoke pending",
              "model_requests_made": 0, "server_requests_made": 0,
              "packet": packet, "reset_sources": sources,
              "unit_test_command": command, "unit_test_exit_code": result.returncode,
              "replay_previews": previews, "reset_preview": reset_preview, "trial_preview": trial_preview, "smoke_preview": smoke_preview, "phase_preview": phase_preview,
              "files": {str(path.relative_to(ROOT)): sha(path.read_bytes()) for path in paths}}
    (ROOT / "cpu-validation.json").write_bytes(json_bytes(report))
    print(result.stdout.decode().strip())
    print(json.dumps({"packet": packet, "reset_sources": sources, "previews": len(previews),
                      "server_requests_made": 0}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock-binary", type=Path)
    parser.add_argument("--fixed-binary", type=Path)
    args = parser.parse_args()
    check(args.stock_binary, args.fixed_binary)
