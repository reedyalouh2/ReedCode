#!/usr/bin/env bash
set -euo pipefail
bundle_dir="${1:?Usage: setup-frontends.sh BUNDLE_DIR STUDY_DIR}"
study_dir="${2:?Usage: setup-frontends.sh BUNDLE_DIR STUDY_DIR}"
mkdir -p "$study_dir/setup"
python3 - "$bundle_dir" <<'PY'
from pathlib import Path
import hashlib, json, platform, sys
root = Path(sys.argv[1]).resolve()
assert platform.system() == 'Linux' and platform.machine() == 'x86_64'
manifest = json.loads((root / 'artifact-manifest.json').read_text())
assert manifest['revision'] == 'f5d3353e2167bb0f0d729085eb5bc9183bf4b222'
assert set(manifest['wheels']) == {'common', 'stock', 'fixed'}
for key, row in manifest['wheels'].items():
    path = (root / row['path']).resolve()
    assert root in path.parents and path.is_file(), key
    assert hashlib.sha256(path.read_bytes()).hexdigest() == row['sha256'], key
print('Artifact hashes verified')
PY
python3 -c 'import importlib.metadata as m; assert m.version("ai-dynamo") == "1.5.0"; assert m.version("ai-dynamo-runtime") == "1.5.0"; assert m.version("vllm") == "0.28.0"; print("Pinned backend packages verified")' > "$study_dir/setup/backend-packages.txt"
for variant in stock fixed; do
  environment_dir="$study_dir/frontend-$variant"
  test ! -e "$environment_dir"
  python3 -m venv --system-site-packages "$environment_dir"
  "$environment_dir/bin/python" -m pip install --no-deps \
    "$bundle_dir/common/ai_dynamo-1.6.0-py3-none-any.whl" \
    "$bundle_dir/$variant/ai_dynamo_runtime-1.6.0-cp310-abi3-linux_x86_64.whl" \
    > "$study_dir/setup/install-$variant.log" 2>&1
  "$environment_dir/bin/python" -c 'import importlib.metadata as m, sys; from pathlib import Path; import dynamo._core; import dynamo.frontend.main; assert m.version("ai-dynamo") == "1.6.0"; assert m.version("ai-dynamo-runtime") == "1.6.0"; assert Path(dynamo._core.__file__).is_relative_to(sys.prefix); assert Path(dynamo.frontend.main.__file__).is_relative_to(sys.prefix); print("Frontend import passed", dynamo._core.__file__, dynamo.frontend.main.__file__)' \
    > "$study_dir/setup/import-$variant.log" 2>&1
  "$environment_dir/bin/python" -m pip freeze > "$study_dir/setup/packages-$variant.txt"
done
python3 -c 'import importlib.metadata as m; assert m.version("ai-dynamo") == "1.5.0"; assert m.version("ai-dynamo-runtime") == "1.5.0"; assert m.version("vllm") == "0.28.0"; print("Backend packages unchanged")' >> "$study_dir/setup/backend-packages.txt"
