#!/bin/sh
set -eu
python3 /readiness/transport_relay.py --port 18000 --host-port 18002 &
parity_relay_pid=$!
trap 'kill "$parity_relay_pid" 2>/dev/null || true' EXIT
python3 /readiness/run_session.py --client codex --binary /codex \
    --workspace /study --prompt-dir /prompts --container-isolated \
    --upstream http://127.0.0.1:18000 --seconds 1500 --max-calls 15 "$@"
