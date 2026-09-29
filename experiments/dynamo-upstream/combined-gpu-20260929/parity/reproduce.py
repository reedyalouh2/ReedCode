#!/usr/bin/env python3
"""Rebuild the parity reports from unchanged HTTP and runtime captures."""
import argparse
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tarfile

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
TOOLS = REPO / 'experiments/dynamo-upstream/parity-run-1'


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path):
    return json.loads(path.read_text())


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded(value))


def relative(path, base):
    return os.path.relpath(path.resolve(), base.resolve())


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def unpack_clients(output):
    manifest = read(HERE / 'client-captures.json')
    raw = (HERE / 'client-captures.tar.gz').read_bytes()
    if digest(raw) != manifest['archive_sha256']:
        raise ValueError('Client archive hash mismatch')
    output.mkdir()
    with tarfile.open(HERE / 'client-captures.tar.gz') as archive:
        if set(archive.getnames()) != set(manifest['files']):
            raise ValueError('Client archive member list mismatch')
        for member in archive.getmembers():
            path = output / member.name
            if not member.isfile() or not path.resolve().is_relative_to(output.resolve()):
                raise ValueError('Unsafe client archive member')
            raw = archive.extractfile(member).read()
            if digest(raw) != manifest['files'][member.name]:
                raise ValueError('Client archive member hash mismatch: ' + member.name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
    return manifest


def combine_controllers(case, clients, output):
    client = case['client']
    sources = [clients / name for name in case['segments']]
    controllers = [read(p / 'result.json') for p in sources]
    first = controllers[0]
    if any(c['session_id'] != first['session_id'] for c in controllers):
        raise ValueError('Controller segments changed session identity')
    turns = list(first['turns'])
    for folder, controller in zip(sources[1:], controllers[1:]):
        if controller.get('original_sha256') != digest((sources[0] / 'result.json').read_bytes()):
            raise ValueError('Continuation is not bound to the original result')
        for turn in controller['turns']:
            if turn['user_turn'] != len(turns) + 1:
                raise ValueError('Continuation user turns are out of order')
            turns.append(turn)
    if any(a['ended_monotonic_ns'] >= b['started_monotonic_ns'] for a, b in zip(turns, turns[1:])):
        raise ValueError('Controller segments overlap or reset the capture clock')
    output.mkdir()
    combined = {**first, 'turns': turns, 'derived_controller_view': True,
                'original_failure_retained': first.get('completed') is not True,
                'source_results': [{'path': relative(p / 'result.json', output),
                                    'sha256': digest((p / 'result.json').read_bytes())} for p in sources]}
    if len(sources) > 1:
        combined['completed'] = False
        combined['stop_reason'] = 'original_failure_and_recorded_followup_attempts'
        combined['continuation_interventions'] = [c.get('intervention') for c in controllers[1:]]
    save(output / 'result.json', combined)
    captures = []
    for folder in sources:
        captures.extend(folder.glob('capture/*/metadata.json'))
    captures.sort(key=lambda p: read(p)['started_monotonic_ns'])
    seen, ledger, controller_only, admitted = set(), [], [], {}
    for ordinal, path in enumerate(captures, 1):
        meta = read(path)
        observer = meta.get('observer_x_request_id') or (meta.get('original_x_request_ids') or [None])[0]
        if not observer or observer in seen:
            raise ValueError('Missing or duplicated observer ID')
        seen.add(observer)
        segment = path.parent.parent.parent
        previous_count = admitted.get(segment, 0)
        count = meta.get('admitted_inference_count')
        if not (path.parent / 'request.body').exists():
            if not (meta.get('inference') and previous_count > 0 and count == previous_count
                    and meta.get('complete') is False and not meta.get('unexpected_credentials')
                    and meta.get('request_sha256') is None and meta.get('response_bytes') == 0
                    and meta.get('response_status') is None):
                raise ValueError('Unexplained capture without a request body')
            controller_only.append({'source_capture': relative(path.parent, output),
                'observer_id': observer, 'admitted_inference_count': count,
                'recorded_error': meta.get('error'), 'classification': 'controller_budget_attempt_not_forwarded',
                'evidence': 'Admission count did not advance; no request body was read or saved. The unchanged proxy opens its upstream connection only after saving request.body.'})
            continue
        if meta.get('inference') and count != previous_count + 1:
            raise ValueError('Admitted inference count is not sequential within controller segment')
        admitted[segment] = count
        for name in ('request', 'response'):
            body = path.parent / (name + '.body')
            if not body.exists() or digest(body.read_bytes()) != meta.get(name + '_sha256'):
                raise ValueError('Client body hash mismatch')
        matches = [i + 1 for i, turn in enumerate(turns)
                   if turn['started_monotonic_ns'] <= meta['started_monotonic_ns'] <= turn['ended_monotonic_ns']]
        if len(matches) != 1:
            raise ValueError('Request does not belong to one controller turn')
        # Names are global only in this derived view; raw metadata keeps its original sequence.
        destination = output / 'capture' / f'{ordinal:04d}'
        destination.mkdir(parents=True)
        for name in ('metadata.json', 'request.body', 'response.body'):
            shutil.copyfile(path.parent / name, destination / name)
        error = None
        if meta.get('response_status') != 200:
            try:
                error = json.loads((path.parent / 'response.body').read_bytes())
            except (ValueError, UnicodeDecodeError):
                pass
        ledger.append({'id': client + '-' + destination.name, 'source_capture': relative(path.parent, output),
                       'archive_capture': str(path.parent.relative_to(clients)),
                       'observer_id': observer, 'user_turn': matches[0], 'http_status': meta.get('response_status'),
                       'server_error': error})
    save(output / 'capture-ledger.json', {'requests': ledger,
         'controller_only_attempts': controller_only,
         'controller_source_sha256': digest((TOOLS / 'readiness/capture_proxy.py').read_bytes()),
         'followup_gaps_seconds': [(b['started_monotonic_ns'] - a['ended_monotonic_ns']) / 1e9
                                  for a, b in zip(turns, turns[1:])]})
    return combined, ledger


def lcp(a, b):
    for index, (left, right) in enumerate(zip(a, b)):
        if left != right:
            return index
    return min(len(a), len(b))


def compact_mapping(mapping, wire, destination, wire_path):
    arrays = {r['request_id']: r['input_token_ids'] for r in wire['requests'] if r['kind'] == 'model'}
    inputs = destination / 'backend-inputs.json'
    save(inputs, arrays)
    checksum = digest(inputs.read_bytes())
    old_ref = relative(wire_path, destination)
    compressed_ref = 'wire.json.gz'
    for session in mapping['sessions']:
        for entry in session['requests']:
            if entry.get('backend'):
                spec = entry['backend']
                spec.update(path='backend-inputs.json', sha256=checksum,
                            token_ids_pointer='/' + spec['request_id'])
            entry['link_evidence'] = [v.replace(old_ref, compressed_ref) for v in entry['link_evidence']]
            if entry.get('backend_usage'):
                entry['backend_usage']['evidence'] = [v.replace(old_ref, compressed_ref)
                                                      for v in entry['backend_usage']['evidence']]
    mapping['join'].update(wire='wire.json.gz', wire_encoding='gzip',
                           decoded_wire_sha256=digest(wire_path.read_bytes()),
                           wire_sha256=digest((destination / 'wire.json.gz').read_bytes()))


def first_message_change(before, after):
    def text(message):
        content = message.get('content', '')
        return content if isinstance(content, str) else ''.join(
            block.get('text', '') for block in content if block.get('type') == 'text')

    for index, (old, new) in enumerate(zip(before['messages'], after['messages'])):
        old_text, new_text = text(old), text(new)
        if old_text == new_text:
            continue
        removed = [block['text'] for block in old.get('content', [])
                   if isinstance(block, dict) and block.get('type') == 'text'
                   and 'The following skills are available for use with the Skill tool:' in block['text']
                   and block['text'] not in new_text] if isinstance(old.get('content'), list) else []
        split = lcp(old_text, new_text)
        return {'message_index': index, 'role': old['role'],
                'old_text_sha256': digest(old_text.encode()), 'new_text_sha256': digest(new_text.encode()),
                'first_text_divergence_character': split,
                'old_excerpt': old_text[max(0, split - 20):split + 200],
                'new_excerpt': new_text[max(0, split - 20):split + 200],
                'removed_skill_reminder_characters': sum(map(len, removed)),
                'scope': 'First changed concatenated text blocks at the same raw message index; later changes are not enumerated.'}
    return None


def diagnostics(case, mapping, wire, destination):
    by_id = {r['request_id']: r for r in wire['requests']}
    entries = mapping['sessions'][0]['requests']
    history, generated, rewrites = [], [], []
    for ordinal, entry in enumerate(entries):
        if not entry.get('backend'):
            continue
        row = by_id[entry['backend']['request_id']]
        ids = row['input_token_ids']
        compatible = max((16 * (lcp(ids, old['input_token_ids'] + old['output_token_ids']) // 16)
                          for old in history), default=0)
        generated.append({'id': entry['id'], 'cached_tokens': entry.get('backend_usage', {}).get('cached_tokens'),
                          'input_plus_generated_prefix_tokens': compatible})
        if ordinal and entry['user_turn'] != entries[ordinal - 1]['user_turn']:
            previous = entries[ordinal - 1]
            if previous.get('backend'):
                old = by_id[previous['backend']['request_id']]
                split = lcp(old['input_token_ids'], ids)
                before = read(destination / previous['capture'] / 'request.body')
                after = read(destination / entry['capture'] / 'request.body')
                unchanged = (after.get('input', [])[:len(before.get('input', []))] == before.get('input', [])
                             if case['client'] == 'codex' else None)
                rewrites.append({'id': entry['id'], 'reference_id': previous['id'],
                    'user_turn': entry['user_turn'], 'raw_responses_input_prefix_unchanged': unchanged,
                    'first_engine_token_divergence': split,
                    'previous_input_tokens': len(old['input_token_ids']), 'current_input_tokens': len(ids),
                    'old_input_suffix_exposure_tokens': len(old['input_token_ids']) - split,
                    'old_complete_block_suffix_exposure_tokens': 16 * (len(old['input_token_ids']) // 16) - 16 * (split // 16),
                    'old_http_sha256': digest((destination / previous['capture'] / 'request.body').read_bytes()),
                    'new_http_sha256': digest((destination / entry['capture'] / 'request.body').read_bytes()),
                    'prior_reasoning_items_preserved': sum(item.get('type') == 'reasoning' for item in before.get('input', [])) if unchanged else None,
                    'raw_message_change': first_message_change(before, after) if case['client'] == 'claude' else None,
                    'old_token_window': old['input_token_ids'][max(0, split - 4):split + 24],
                    'new_token_window': ids[max(0, split - 4):split + 24]})
        history.append(row)
    save(destination / 'generated-prefix-diagnostic.json', {'requests': generated,
         'scope': 'Block LCP against earlier captured input plus actual returned generation; separate from the frozen input-only reference.'})
    save(destination / 'history-rewrite-diagnostic.json', {'requests': rewrites,
         'scope': 'The old input suffix after a cross-turn divergence. This is not avoidable prefill work or measured GPU residency.'})


def run_case(case, raw, clients, output, decoder, joiner, reporter):
    destination = output / case['name']
    destination.mkdir()
    combined, ledger = combine_controllers(case, clients, destination / 'client')
    folder = raw / case['pod_directory']
    final = read(folder / 'capture-final.json')
    if not final.get('zero_drops_verified') or final.get('packets_dropped') != 0:
        raise ValueError('Capture lacks final zero-drop counters')
    wire = decoder.decode((folder / 'wire.pcap').read_bytes(), local_addresses=['172.24.0.2'])
    logs = [raw / name for name in case['frontend_logs']]
    decoder.link_frontend_logs(wire, [(str(p.resolve()), p.read_bytes()) for p in logs])
    wire_path = destination / '_wire.json'
    wire_path.write_bytes(encoded(wire))
    (destination / 'wire.json.gz').write_bytes(gzip.compress(wire_path.read_bytes(), mtime=0))
    mapping_path = destination / 'mapping.json'
    mapping = joiner.build_mapping(wire_path, {case['client']: destination / 'client'}, case['worker_id'],
                case['epoch'], raw / case['identity_file'], mapping_path, main_reviewed=True)
    blocked = read(destination / 'client/capture-ledger.json')['controller_only_attempts']
    linked_observers = {row.get('http_link', {}).get('http_request_id') for row in wire['requests']}
    if any(row['observer_id'] in linked_observers for row in blocked):
        raise ValueError('A supposedly blocked controller attempt reached the runtime')
    compact_mapping(mapping, wire, destination, wire_path)
    save(mapping_path, mapping)
    report = reporter.build_report(mapping, destination)
    save(destination / 'report.json', report)
    (destination / 'report.md').write_text(reporter.markdown(report))
    diagnostics(case, mapping, wire, destination)
    used = {entry['backend']['request_id'] for entry in mapping['sessions'][0]['requests'] if entry.get('backend')}
    account = []
    for row in wire['requests']:
        observer = row.get('http_link', {}).get('http_request_id')
        disposition = ('control' if row['kind'] == 'control' else 'measured' if row['request_id'] in used else
                       'reset_canary' if observer and observer.startswith('parity-reset-') else
                       'smoke' if observer and 'smoke' in observer else 'unexplained')
        account.append({'runtime_request_id': row['request_id'], 'observer_id': observer,
                        'kind': row['kind'], 'disposition': disposition, 'complete': row.get('complete')})
    if any(row['disposition'] == 'unexplained' for row in account):
        raise ValueError('Unexplained model request in epoch')
    save(destination / 'capture-accounting.json', {'requests': account, 'capture_final': final,
         'controller_only_attempts': blocked,
         'unlinked_http_ids': mapping['join']['unlinked_capture_ids']})
    save(destination / 'provenance.json', {'configuration': case,
         'sources': {relative(p, destination): digest(p.read_bytes()) for p in
                     [folder / 'wire.pcap', folder / 'capture-final.json', raw / case['identity_file'], *logs]},
         'controller_completed': combined.get('completed'), 'controller_stop_reason': combined.get('stop_reason'),
         'derived_controller': True, 'all_raw_client_responses_retained': True})
    ledger_by_id = {row['id']: row for row in ledger}
    compact_requests = []
    for row in report['requests']:
        entry = ledger_by_id[row['id']]
        compact_requests.append({**{key: row[key] for key in
             ('id', 'user_turn', 'group', 'http_status', 'usable', 'input_tokens',
              'cached_tokens', 'ideal_tokens', 'reference_request_ids')},
             'archive_capture': entry['archive_capture'],
             'observer_id': entry['observer_id'], 'server_error': entry['server_error'],
             'backend_request_id': row.get('backend', {}).get('request_id') if row.get('backend') else None,
             'http_body_sha256': row['evidence_hashes']})
    save(destination / 'compact-report.json', {
         'configuration': case['configuration'], 'worker_id': case['worker_id'], 'epoch': case['epoch'],
         'client_archive_sha256': digest((HERE / 'client-captures.tar.gz').read_bytes()),
         'controller_completed': combined.get('completed'), 'controller_stop_reason': combined.get('stop_reason'),
         'followup_gaps_seconds': read(destination / 'client/capture-ledger.json')['followup_gaps_seconds'],
         'capture_final': final, 'sessions': report['sessions'], 'requests': compact_requests,
         'controller_only_attempts': [{key: value for key, value in row.items() if key != 'source_capture'} for row in blocked],
         'baseline': 'Input-only block LCP against earlier completed requests in the same session and worker epoch.'})
    wire_path.unlink()
    return {'name': case['name'], 'configuration': case['configuration'], 'sessions': report['sessions'],
            'controller_completed': combined.get('completed'), 'controller_stop_reason': combined.get('stop_reason')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--case', action='append')
    args = parser.parse_args()
    args.raw = args.raw.resolve()
    args.output = args.output.resolve()
    args.output.mkdir(exist_ok=False)
    clients = args.output / 'raw-clients'
    manifest = unpack_clients(clients)
    decoder = load_module('parity_wire_decode', TOOLS / 'wire-capture/decode.py')
    joiner = load_module('parity_wire_join', TOOLS / 'join_wire.py')
    reporter = load_module('parity_report', TOOLS / 'report.py')
    cases = read(HERE / 'cases.json')
    if args.case and set(args.case) - {case['name'] for case in cases}:
        raise ValueError('Unknown case name')
    summaries = [run_case(case, args.raw, clients, args.output, decoder, joiner, reporter)
                 for case in cases if not args.case or case['name'] in args.case]
    save(args.output / 'summary.json', {'cases': summaries, 'pooled_across_configurations': False,
         'client_archive_sha256': manifest['archive_sha256'],
         'tool_hashes': {str(p.relative_to(REPO)): digest(p.read_bytes()) for p in
                        [Path(__file__), TOOLS / 'wire-capture/decode.py', TOOLS / 'join_wire.py', TOOLS / 'report.py']}})
    print(json.dumps({'output': str(args.output), 'cases': [row['name'] for row in summaries]}))


if __name__ == '__main__':
    main()
