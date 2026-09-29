set -euo pipefail
variant="$1"
cp -a /work/venvs/backend "/work/venvs/main-$variant"
"/work/venvs/main-$variant/bin/python" -m pip install --no-deps --force-reinstall "/work/artifacts/$variant/ai_dynamo_runtime-1.6.0-cp310-abi3-linux_x86_64.whl" /work/artifacts/common/ai_dynamo-1.6.0-py3-none-any.whl
"/work/venvs/main-$variant/bin/python" -c 'import importlib.metadata as m; import dynamo._core; import dynamo.frontend.main; print("MAIN_IMPORT_OK", m.version("ai-dynamo-runtime"),m.version("ai-dynamo"))'
ldd "/work/venvs/main-$variant/lib/python3.11/site-packages/dynamo/_core.abi3.so"
