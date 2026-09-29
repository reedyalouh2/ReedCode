set -euo pipefail
for variant in stock fixed; do
  bash /work/run-installed-smoke.sh "$variant" > "/work/logs/main-$variant-warmup.log" 2>&1
done
PROTOCOL_OUTPUT=/work/protocol-main-fixed FRONTEND_PYTHON=/work/venvs/main-fixed/bin/python \
  /work/venvs/backend/bin/python /work/run-stock-protocol.py > /work/logs/main-fixed-protocol.log 2>&1
