"""Reproduce the controlled prefill report from the final extracted pod evidence."""

import argparse
import gzip
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
READINESS = REPO / 'experiments/dynamo-upstream/001-speculative-prefill/gpu-readiness'
DECODER = REPO / 'experiments/dynamo-upstream/parity-run-1/wire-capture/decode.py'
EPOCH = 'prefill/epochs/267e605d0db348158ba0f551f5ac87e8'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def run(raw, output):
    raw, output = raw.resolve(), output.resolve()
    if output.is_relative_to(raw) or output.exists():
        raise ValueError('Choose a new output directory outside raw evidence')
    for package, expected in (('msgpack', '1.1.1'), ('xxhash', '3.5.0')):
        if importlib.metadata.version(package) != expected:
            raise ValueError('Use the documented package version: ' + package + '==' + expected)
    sys.path.insert(0, str(READINESS))
    import analyze_snapshot

    manifest, _ = analyze_snapshot.verify_snapshot(raw)
    epoch = raw / EPOCH
    trials = sorted((epoch / 'trials').glob('*/trial.json'))
    if len(trials) != 18 or any(json.loads(path.read_text())['status'] != 'replay_completed' for path in trials):
        raise ValueError('The completed study requires all 18 trial records')
    logs = [epoch / 'frontend.log']
    for trial in trials:
        recorded = json.loads(trial.read_text())['frontend']['proof_file']
        logs.append(analyze_snapshot.relocate(raw, recorded).parent / 'frontend.log')
    if len(set(logs)) != len(logs):
        raise ValueError('Repeated frontend log')
    for path in logs + [epoch / 'wire.pcap']:
        if str(path.relative_to(raw)) not in manifest['files']:
            raise ValueError('Capture evidence is absent from the verified manifest')
    spec = importlib.util.spec_from_file_location('prefill_wire_decoder', DECODER)
    decoder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(decoder)
    wire = decoder.decode((epoch / 'wire.pcap').read_bytes(), local_addresses=['172.24.0.2'])
    decoder.link_frontend_logs(wire, [(str(path.relative_to(raw)), path.read_bytes()) for path in logs])
    wire_bytes = (json.dumps(wire, indent=2) + '\n').encode()
    output.mkdir(parents=True)
    (output / 'wire.json.gz').write_bytes(gzip.compress(wire_bytes, mtime=0))
    with tempfile.TemporaryDirectory(prefix='reedcode-prefill-report-') as temporary:
        wire_path = Path(temporary) / 'wire.json'
        wire_path.write_bytes(wire_bytes)
        report = analyze_snapshot.analyze(raw, EPOCH, wire_path)
    report['provenance']['extracted_root'] = 'raw/pod'
    report['provenance']['path_base'] = 'combined-gpu-20260929 study directory'
    save(output / 'report.json', report)
    command = [sys.executable, str(HERE / 'summarize.py'), '--raw', str(raw), '--output', str(output),
               '--repo', str(REPO), '--allocation-evidence', str(HERE / 'allocation-source')]
    summary = subprocess.run(command, capture_output=True, text=True)
    (output / 'summary.stdout').write_text(summary.stdout)
    (output / 'summary.stderr').write_text(summary.stderr)
    if summary.returncode:
        raise RuntimeError('Summary validation failed; inspect summary.stderr')
    save(output / 'reproduction.json', {
        'raw_path_base': 'raw/pod', 'selected_epoch': EPOCH, 'verified_raw_files': len(manifest['files']),
        'snapshot_manifest_sha256': sha(raw / 'snapshot-manifest.json'),
        'packages': {name: importlib.metadata.version(name) for name in ('msgpack', 'xxhash')},
        'sources_sha256': {str(path.relative_to(REPO)): sha(path) for path in
            [Path(__file__), HERE / 'summarize.py', DECODER, READINESS / 'analyze_snapshot.py',
             READINESS / 'kv_report.py', READINESS / 'study_report.py', READINESS / 'trial.py']},
        'network_identity_sha256': sha(HERE / 'network-identity.json'),
        'wire_uncompressed_sha256': hashlib.sha256(wire_bytes).hexdigest(),
        'files': {path.name: sha(path) for path in sorted(output.iterdir()) if path.is_file()},
        'raw_files_modified': False})
    return {'planned_trials': 18, 'completed_reports': report['completed_reports'], 'output': str(output)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw', type=Path, required=True, help='Extracted directory containing snapshot-manifest.json')
    parser.add_argument('--output', type=Path, required=True, help='Fresh directory for derived results')
    args = parser.parse_args()
    print(json.dumps(run(args.raw, args.output), indent=2))
