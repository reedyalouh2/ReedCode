#!/bin/bash

python - <<'PY'
from pathlib import Path

path = Path("/app/hello.txt")
assert path.exists(), "hello.txt does not exist"
assert path.read_text().strip() == "Hello, world!", "wrong contents"
PY

if [ $? -eq 0 ]; then
    echo 1 > /logs/verifier/reward.txt
else
    echo 0 > /logs/verifier/reward.txt
fi
