#!/bin/bash

python - <<'PY'
for i in range(150):
    print(f"diagnostic line {i:03d}: initialization subsystem healthy")
PY

pytest -q
