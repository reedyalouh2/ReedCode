#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
exec uv run python run_experiments.py synthetic "$@"
