"""Analyze an extracted pod snapshot without changing its recorded files."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath

from prepare import ROOT
from kv_report import report
from study_report import metrics
from trial import check_frontend_proof


ORIGINAL_ROOT = PurePosixPath('/tmp/reedcode-study')


def sha_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def inside(root, relative):
    relative = PurePosixPath(relative)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Snapshot path is not a safe relative path')
    path = root.joinpath(*relative.parts)
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Snapshot path leaves the extracted root')
    return path


def relocate(root, recorded):
    path = PurePosixPath(recorded)
    if not path.is_absolute() or not path.is_relative_to(ORIGINAL_ROOT):
        raise ValueError('Recorded path is outside the original study root')
    return inside(root, str(path.relative_to(ORIGINAL_ROOT)))


def verify_snapshot(root):
    manifest_path = root / 'snapshot-manifest.json'
    manifest = json.loads(manifest_path.read_text())
    for name, digest in manifest['files'].items():
        path = inside(root, name)
        if not path.is_file() or sha_file(path) != digest:
            raise ValueError('Snapshot member missing or changed: ' + name)
    return manifest, sha_file(manifest_path)


def analyze(root, epoch=None, wire=None):
    root = root.resolve()
    manifest, manifest_hash = verify_snapshot(root)
    listed = set(manifest['files'])

    def verified(path):
        relative = str(path.relative_to(root))
        if relative not in listed:
            raise ValueError('Evidence is absent from the verified snapshot manifest: ' + relative)
        return path

    candidates = sorted({str(PurePosixPath(*PurePosixPath(name).parts[:3])) for name in listed
                         if len(PurePosixPath(name).parts) >= 4 and PurePosixPath(name).parts[:2] == ('prefill', 'epochs')})
    if epoch is None:
        if len(candidates) > 1:
            raise ValueError('Multiple prefill epochs exist; select one with --epoch')
        selected = inside(root, candidates[0]) if candidates else None
    else:
        selected = relocate(root, epoch) if PurePosixPath(epoch).is_absolute() else inside(root, epoch)
        if str(selected.relative_to(root)) not in candidates:
            raise ValueError('Selected prefill epoch is absent from the verified snapshot')

    wire_error = 'No decoded wire evidence was supplied'
    wire_hash = None
    if wire is not None:
        if selected is None:
            raise ValueError('Decoded wire evidence has no selected prefill epoch')
        decoded = json.loads(wire.read_text())
        pcap = verified(selected / 'wire.pcap')
        if decoded['pcap_sha256'] != sha_file(pcap):
            raise ValueError('Decoded wire hash differs from the verified epoch PCAP')
        capture = json.loads(verified(selected / 'capture-final.json').read_text())
        if capture.get('zero_drops_verified') is not True:
            raise ValueError('Stopped capture lacks a verified zero-drop record')
        wire_hash, wire_error = sha_file(wire), None

    schedule_path = ROOT / 'fixtures/schedule.json'
    schedule = json.loads(schedule_path.read_text())
    expected = {(s, r, c) for s in ('short', 'long') for r in (1, 2, 3) for c in ('off', 'stock', 'fixed')}
    if len(schedule) != 18 or {(row['session'], row['repeat'], row['condition']) for row in schedule} != expected:
        raise ValueError('The frozen schedule does not contain the 18 planned cells')
    cells, completed, grouped, planned_directories = [], [], {}, set()
    for index, planned in enumerate(schedule):
        key = planned['session'], planned['repeat'], planned['condition']
        name = f'{key[0]}-{key[2]}-r{key[1]}'
        cell = {'schedule_index': index, 'session': key[0], 'repeat': key[1], 'condition': key[2],
                'status': 'not_started'}
        directory = selected / 'trials' / name if selected else None
        if directory:
            planned_directories.add(str(directory.relative_to(root)))
            cell['directory'] = str(directory.relative_to(root))
        path = directory / 'trial.json' if directory else None
        if path is not None and path.exists():
            try:
                trial = json.loads(verified(path).read_text())
                if (trial.get('session'), trial.get('repeat'), trial.get('condition')) != key:
                    raise ValueError('Trial identity differs from its scheduled directory')
                cell['recorded_status'] = trial.get('status')
                cell['recorded_error'] = trial.get('error')
                if trial.get('status') != 'replay_completed':
                    cell['status'] = 'incomplete'
                elif wire_error:
                    cell.update(status='analysis_unavailable', error=wire_error)
                else:
                    frontend = dict(trial['frontend'])
                    mapped_proof = verified(relocate(root, frontend['proof_file']))
                    frontend['proof_file'] = str(mapped_proof)
                    check_frontend_proof(frontend)
                    for member in ('kv-config.json', 'kv-frames.jsonl', 'replay/run.json', 'replay/requests.jsonl'):
                        verified(directory / member)
                    replay = json.loads((directory / 'replay/run.json').read_text())
                    for member in replay['files']:
                        verified(inside(directory / 'replay', member))
                    result = report(directory, wire)
                    if (result['session'], result['repeat'], result['condition']) != key:
                        raise ValueError('Replay identity differs from its scheduled trial')
                    result['directory'] = str(directory.relative_to(root))
                    completed.append(result)
                    grouped[key] = result
                    cell.update(status='completed', frontend_proof=str(mapped_proof.relative_to(root)))
            except (ValueError, KeyError, OSError, TypeError) as error:
                cell.update(status='analysis_failed', error_type=type(error).__name__, error=str(error))
        elif directory is not None and directory.exists():
            cell.update(status='incomplete', error='Trial directory exists without a completed trial manifest')
        cells.append(cell)
    pairs = []
    for session in ('short', 'long'):
        for repeat in (1, 2, 3):
            for before, after in (('off', 'stock'), ('stock', 'fixed'), ('off', 'fixed')):
                first, last = grouped.get((session, repeat, before)), grouped.get((session, repeat, after))
                pair = {'session': session, 'repeat': repeat, 'comparison': after + ' minus ' + before,
                        'complete_pair': first is not None and last is not None}
                if pair['complete_pair']:
                    left, right = metrics(first), metrics(last)
                    pair.update(before=left, after=right, difference={
                        k: right[k] - left[k] if left[k] is not None and right[k] is not None else None for k in left})
                pairs.append(pair)
    discovered = {str(PurePosixPath(name).parent) for name in listed
                  if selected and name.startswith(str(selected.relative_to(root)) + '/trials/') and name.endswith('/trial.json')}
    return {'planned_trials': 18, 'completed_reports': len(completed), 'cells': cells,
            'unplanned_trial_directories': sorted(discovered - planned_directories),
            'trials': completed, 'paired_differences': pairs,
            'provenance': {'original_root': str(ORIGINAL_ROOT), 'extracted_root': str(root),
                'selected_epoch': str(selected.relative_to(root)) if selected else None,
                'snapshot_manifest_sha256': manifest_hash,
                'snapshot_final_after_process_stop': manifest.get('final_after_process_stop'),
                'schedule_sha256': sha_file(schedule_path), 'wire_sha256': wire_hash,
                'analysis_script_sha256': sha_file(Path(__file__)),
                'analysis_sources_sha256': {name: sha_file(ROOT / name) for name in
                    ('analyze_snapshot.py', 'kv_report.py', 'study_report.py', 'trial.py')},
                'raw_files_modified': False},
            'scope': 'Controlled builder replay. Missing and incomplete planned cells remain explicit; physical KV bytes, throughput and shared-server effects remain unmeasured.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pod', type=Path, required=True, help='Extracted directory containing snapshot-manifest.json')
    parser.add_argument('--epoch', help='Original absolute path or extracted-root-relative prefill epoch')
    parser.add_argument('--wire-decoded', type=Path)
    parser.add_argument('--output', type=Path, required=True, help='A new derived JSON file outside the raw snapshot')
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(args.pod.resolve()) or args.output.exists():
        parser.error('Choose a new output file outside the raw snapshot')
    result = analyze(args.pod, args.epoch, args.wire_decoded)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as output:
        json.dump(result, output, indent=2)
        output.write('\n')
    print(json.dumps({'planned_trials': 18, 'completed_reports': result['completed_reports'], 'output': str(args.output)}))


if __name__ == '__main__':
    main()
