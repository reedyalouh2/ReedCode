"""Capture only the isolated container's loopback while the stock CPU smoke runs."""

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time


ROOT = Path(__file__).resolve().parent


def main():
    if os.environ.get("WIRE_CAPTURE_ISOLATED") != "network-none":
        raise RuntimeError("Run in the documented Docker container with --network none")
    output = Path(os.environ["PROTOCOL_OUTPUT"])
    output.mkdir(parents=True, exist_ok=False)
    command = ["tcpdump", "--immediate-mode", "-i", "lo", "-s", "0", "-U", "-B", "4096", "-w", str(output / "runtime.pcap"),
               "tcp", "and", "not", "port", "18000"]
    result = None
    with (output / "tcpdump.log").open("w") as log:
        capture = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 10
            while "listening on lo" not in (output / "tcpdump.log").read_text():
                if capture.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("tcpdump did not become ready")
                time.sleep(.05)
            with (output / "protocol.log").open("w") as protocol:
                result = subprocess.run(["/work/venvs/backend/bin/python", str(ROOT / "protocol_runner.py")],
                                        stdout=protocol, stderr=subprocess.STDOUT, timeout=240)
            time.sleep(.25)
        finally:
            capture.send_signal(signal.SIGINT)
            capture.wait(timeout=10)
    counts = {name: int(re.search(r"(\d+) packets " + phrase, (output / "tcpdump.log").read_text()).group(1))
              for name, phrase in (("captured", "captured"), ("dropped", "dropped by kernel"))}
    (output / "capture.json").write_text(json.dumps({
        "command": command, "tcpdump_exit_code": capture.returncode,
        "protocol_exit_code": None if result is None else result.returncode,
        "client": os.environ["PROTOCOL_CLIENT"], "network": "Docker --network none; loopback only",
        "backend": "CPU callback returning fixed tokens; no model loaded", "gpu_requests": 0,
        "tcpdump_version": subprocess.check_output(["tcpdump", "--version"]).decode(), "packet_counts": counts,
    }, indent=2) + "\n")
    if result is None or result.returncode or capture.returncode or counts["captured"] == 0 or counts["dropped"]:
        raise RuntimeError("Capture or CPU protocol failed; preserve output for inspection")
    print(str(output))


if __name__ == "__main__":
    main()
