set -euo pipefail
python3 -m venv /work/venvs/backend
/work/venvs/backend/bin/pip install --disable-pip-version-check /work/backend-wheels/ai_dynamo_runtime-1.5.0-*.whl 'aiohttp>=3.14.3,<4' 'kubernetes>=32.0.1,<33' 'msgspec>=0.19' 'prometheus-client>=0.23.1,<1' 'pyzmq>=26' 'tqdm>=4' 'transformers>=4.56' 'typing-extensions>=4.10' 'zstandard>=0.23,<1'
/work/venvs/backend/bin/pip install --no-deps /work/backend-wheels/ai_dynamo-1.5.0-*.whl
/work/venvs/backend/bin/python - <<'PY'
import importlib.metadata as m
import dynamo._core
import dynamo.runtime
import dynamo.llm
import dynamo.frontend.main
print('PINNED_IMAGE_IMPORT_OK', m.version('ai-dynamo-runtime'), m.version('ai-dynamo'))
PY
/work/venvs/backend/bin/python -m dynamo.frontend --help > /work/logs/backend-frontend-help.txt
/work/venvs/backend/bin/pip freeze > /work/logs/backend-python-packages.txt
