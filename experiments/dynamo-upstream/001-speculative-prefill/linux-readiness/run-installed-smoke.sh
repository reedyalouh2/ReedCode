set -euo pipefail
variant="$1"
export WARMUP_VARIANT="$variant"
export FRONTEND_PYTHON="/work/venvs/main-$variant/bin/python"
for example in text tool; do
  for hint in off on; do
    export PROTOCOL_OUTPUT="/work/installed-smoke-$variant-$example-$hint"
    export WARMUP_CASE="$example"
    export WARMUP_HINT="$hint"
    /work/venvs/backend/bin/python /work/run-warmup-protocol.py
  done
done
