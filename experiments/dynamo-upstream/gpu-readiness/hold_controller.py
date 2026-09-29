"""Keep controller cleanup armed while the saved study commands run."""

import argparse
import json
from pathlib import Path
import time


def hold(state_path):
    state = json.loads(state_path.read_text())
    directory = state_path.parent
    end = state["allocation_started_unix"] + 140 * 60
    while time.time() < end:
        if any((directory / name).exists() for name in ("study_done", "deleted")):
            return
        (directory / "controller-heartbeat.json").write_text(json.dumps({"at_unix": time.time()}))
        time.sleep(5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    hold(parser.parse_args().state)
