"""Verify the evidence and rebuild both GPU reports without a server."""

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
import tarfile


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read(path):
    return json.loads(path.read_text())


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def checked_path(base, name):
    path = base / name
    if Path(name).is_absolute() or '..' in Path(name).parts or not path.resolve().is_relative_to(base.resolve()):
        raise ValueError('Unsafe evidence path: ' + name)
    return path


def verify_files(base, files):
    for name, expected in files.items():
        path = checked_path(base, name)
        if not path.is_file() or sha(path) != expected:
            raise ValueError('Evidence hash mismatch: ' + name)


def unpack(output):
    output.mkdir()
    archive_path = HERE / 'raw/pod.tar.gz'
    with tarfile.open(archive_path) as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if len(set(names)) != len(names):
            raise ValueError('Duplicated archive member')
        for member in members:
            path = checked_path(output, member.name)
            if not member.isfile():
                raise ValueError('Only regular evidence files are allowed')
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, path.open('xb') as target:
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)
    manifest = read(output / 'snapshot-manifest.json')
    if not manifest.get('final_after_process_stop'):
        raise ValueError('Expected a final stopped capture')
    if set(names) != set(manifest['files']) | {'snapshot-manifest.json'}:
        raise ValueError('Raw archive and manifest membership differ')
    verify_files(output, manifest['files'])
    return len(manifest['files'])


def run_piece(name, raw, output):
    command = [sys.executable, str(HERE / name / 'reproduce.py'),
               '--raw', str(raw), '--output', str(output / name)]
    result = subprocess.run(command, capture_output=True, text=True)
    (output / (name + '.stdout')).write_text(result.stdout)
    (output / (name + '.stderr')).write_text(result.stderr)
    if result.returncode:
        raise RuntimeError(name + ' reproduction failed; inspect its stderr')


def compare_results(output):
    expected = read(HERE / 'prefill/results/summary.json')
    actual = read(output / 'prefill/summary.json')
    if actual != expected:
        raise ValueError('Prefill summary differs from the saved result')
    cases = []
    causes = ['rendering', 'reasoning round trip', 'tool-call round trip',
              'tool or schema ordering', 'harness-side edits', 'eviction', 'other']
    for case in read(HERE / 'parity/cases.json'):
        name = case['name']
        for filename in ('compact-report.json', 'history-rewrite-diagnostic.json',
                         'generated-prefix-diagnostic.json'):
            if read(output / 'parity' / name / filename) != read(HERE / 'parity/results' / name / filename):
                raise ValueError('Parity result changed: ' + name + '/' + filename)
        report = read(output / 'parity' / name / 'compact-report.json')
        generated = read(output / 'parity' / name / 'generated-prefix-diagnostic.json')['requests']
        if any(row['cached_tokens'] != row['input_plus_generated_prefix_tokens'] for row in generated):
            raise ValueError('Input-plus-generation cache accounting differs')
        misses = sum(max(0, row['ideal_tokens'] - row['cached_tokens'])
                     for row in report['requests'] if row['usable'])
        if misses:
            raise ValueError('New compatible-prefix misses need attribution')
        rewrites = read(output / 'parity' / name / 'history-rewrite-diagnostic.json')['requests']
        history = []
        for row in rewrites:
            if case['client'] == 'codex':
                if row['raw_responses_input_prefix_unchanged'] is not True:
                    raise ValueError('Codex historical input changed')
                cause = 'reasoning round trip'
            else:
                if not row['raw_message_change']['removed_skill_reminder_characters']:
                    raise ValueError('Claude reminder-removal evidence missing')
                cause = 'harness-side edits'
            history.append({'request': row['id'], 'cause': cause,
                            'first_divergence': row['first_engine_token_divergence'],
                            'old_suffix_tokens': row['old_input_suffix_exposure_tokens']})
        cases.append({'case': name, 'sessions': report['sessions'],
                      'compatible_prefix_misses_by_cause': dict.fromkeys(causes, 0),
                      'history_boundaries': history,
                      'history_exposure_tokens_by_cause': {
                          cause: sum(row['old_suffix_tokens'] for row in history if row['cause'] == cause)
                          for cause in causes}})
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() or output.is_relative_to(HERE):
        raise ValueError('Choose a fresh output directory outside the evidence packet')
    for name, expected in (('msgpack', '1.1.1'), ('xxhash', '3.5.0')):
        if importlib.metadata.version(name) != expected:
            raise ValueError('Use ' + name + '==' + expected)
    manifest = read(HERE / 'manifest.json')
    verify_files(HERE, manifest['files'])
    verify_files(REPO, manifest['repository_sources'])
    output.mkdir(parents=True)
    raw = output / 'raw-pod'
    count = unpack(raw)
    for name in ('prefill', 'parity'):
        run_piece(name, raw, output)
    cases = compare_results(output)
    result = {'raw_files_verified': count, 'prefill_trials': 18,
              'parity_cases': cases, 'saved_results_match': True,
              'study_manifest_sha256': sha(HERE / 'manifest.json'),
              'history_exposure_is_avoidable_compute': False,
              'model_or_provider_requests': 0}
    save(output / 'verification.json', result)
    print(json.dumps({'output': str(output), 'raw_files_verified': count,
                      'prefill_trials': 18, 'parity_cases': len(cases), 'saved_results_match': True}))


if __name__ == '__main__':
    main()
