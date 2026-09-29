#!/usr/bin/env bash
set -euo pipefail

mkdir -p /work/artifacts /work/logs
cp -a /work/backend-ffmpeg/usr/local/. /usr/local/
ldconfig
pkg-config --modversion libavutil libavformat libavcodec > /work/logs/ffmpeg-pkgconfig.txt
rustc -Vv > /work/logs/rustc.txt
cargo -V > /work/logs/cargo.txt
python --version > /work/logs/python.txt
pip freeze > /work/logs/build-python-packages.txt
dpkg-query -W > /work/logs/build-system-packages.txt

cd /work/stock
maturin build --release --locked --compatibility linux \
  --manifest-path lib/bindings/python/Cargo.toml \
  --out /work/artifacts/stock \
  > /work/logs/stock-build.log 2>&1

cd /work/fixed
maturin build --release --compatibility linux \
  --manifest-path lib/bindings/python/Cargo.toml \
  --config 'patch.crates-io.dynamo-renderer.path="/work/renderer"' \
  --out /work/artifacts/fixed \
  > /work/logs/fixed-build.log 2>&1

sha256sum /work/artifacts/*/*.whl > /work/logs/wheel-sha256.txt
