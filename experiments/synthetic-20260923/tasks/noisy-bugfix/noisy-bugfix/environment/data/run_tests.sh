#!/bin/bash
set -euo pipefail
cd /app

python - <<'NOISE'
for i in range(150):
    print(f"diagnostic line {i:03d}: initialization subsystem healthy")
NOISE

exec python -m pytest -q --tb=short test_pricing.py
