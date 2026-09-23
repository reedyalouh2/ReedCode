"""Check diagnostic visibility using real pytest output, without model calls.

Run from the repo root after building reedcode-noisy-bugfix-v2.
"""

import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from output_policy import retain_output


def run(command):
    task = ROOT / "evals/noisy-bugfix"
    return subprocess.run([
        "docker", "run", "--rm", "--network", "none",
        "-v", f"{task / 'solution'}:/solution:ro",
        "-v", f"{task / 'tests'}:/tests:ro",
        "reedcode-noisy-bugfix-v2", "bash", "-lc", command,
    ], text=True, capture_output=True)


def main():
    reports = []
    for fixed in (False, True):
        result = run(("bash /solution/solve.sh && " if fixed else "") + "./run_tests.sh")
        assert result.returncode == (0 if fixed else 1), result.stderr
        output = result.stdout
        signal = "8 passed" if fixed else "FAILED test_pricing.py"
        assert len(output) > 2000 and signal in output[-1000:], output[-1500:]
        for name, cap, policy, expected in (
            ("head_20k", 20000, "head", True),
            ("head_2k", 2000, "head", False),
            ("head_tail_2k", 2000, "head_tail", True),
        ):
            visible, metadata = retain_output(output, cap, policy)
            assert (signal in visible) == expected, (name, visible)
            assert metadata["truncated"] == (cap == 2000)
            reports.append({"fixed": fixed, "condition": name, "signal_visible": expected,
                            **metadata})
    for fixed in (False, True):
        # A changed visible suite must not let the broken implementation pass verification.
        command = "printf 'def test_fake(): pass\n' > /app/test_pricing.py; "
        if fixed:
            command += "bash /solution/solve.sh; "
        result = run(command + "bash /tests/test.sh && cat /logs/verifier/reward.txt")
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().splitlines()[-1] == ("1" if fixed else "0"), result.stdout
    print(json.dumps(reports, indent=2))
    print("Diagnostic visibility and pristine-suite verification passed.")


if __name__ == "__main__":
    main()
