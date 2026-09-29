set -euo pipefail
deadline=$((SECONDS + 1200))
while [[ ! -f /work/logs/wheel-sha256.txt ]]; do
  if ((SECONDS >= deadline)); then
    echo 'Timed out waiting for the existing fixed-wheel build' >&2
    exit 1
  fi
  sleep 5
done
bash /work/install-main-runtime.sh fixed > /work/logs/main-fixed-import.log 2>&1
bash /work/run-installed-smoke.sh fixed > /work/logs/main-fixed-warmup.log 2>&1
PROTOCOL_OUTPUT=/work/protocol-main-fixed FRONTEND_PYTHON=/work/venvs/main-fixed/bin/python \
  /work/venvs/backend/bin/python /work/run-stock-protocol.py > /work/logs/main-fixed-protocol.log 2>&1
