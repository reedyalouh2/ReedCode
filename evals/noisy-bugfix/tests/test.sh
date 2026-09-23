#!/bin/bash
set -uo pipefail
mkdir -p /logs/verifier
cd /tests

# Run the pristine suite against the agent's implementation.
if PYTHONPATH=/app PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q --tb=short test_pricing.py; then
    echo 1 > /logs/verifier/reward.txt
else
    echo 0 > /logs/verifier/reward.txt
fi
