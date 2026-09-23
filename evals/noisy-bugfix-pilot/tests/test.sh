#!/bin/bash

python - <<'PY'
import sys
sys.path.insert(0, "/app")

from pricing import final_price

assert final_price(100, 20) == 80
assert final_price(50, 0) == 50
assert final_price(75, 100) == 0
PY

if [ $? -eq 0 ]; then
    echo 1 > /logs/verifier/reward.txt
else
    echo 0 > /logs/verifier/reward.txt
fi
