# /// script
# requires-python = ">=3.12"
# dependencies = ["jsonschema==4.25.1"]
# ///
"""Recompute the thinking-control results from saved request records."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile


def comparable(result):
    result = json.loads(json.dumps(result))
    for row in result['rows']:
        row.pop('raw_evidence', None)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    for name, expected in json.loads((root / 'SHA256SUMS.json').read_text()).items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise SystemExit(f'Hash mismatch: {name}')
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    raw = out / 'raw'
    raw.mkdir()
    with tarfile.open(root / 'evidence.tar.gz') as archive:
        archive.extractall(raw, filter='data')
    subprocess.run([
        sys.executable, str(root / 'analyze.py'),
        '--run', str(raw / 'thinking-primary'),
        '--fixtures-root', str(root),
        '--expected-prompts', str(root / 'expected-prompts.json'),
        '--metadata', str(root / 'manifest.json'),
        '--output', str(out / 'analysis'),
    ], check=True)
    actual = json.loads((out / 'analysis/result.json').read_text())
    expected = json.loads((root / 'results.json').read_text())
    if comparable(actual) != comparable(expected):
        raise SystemExit('Analysis differs from the saved results')
    if (out / 'analysis/rows.tsv').read_bytes() != (root / 'rows.tsv').read_bytes():
        raise SystemExit('Request table differs from the saved results')
    print('Verified all 48 requests and 24 pairs.')


if __name__ == '__main__':
    main()
