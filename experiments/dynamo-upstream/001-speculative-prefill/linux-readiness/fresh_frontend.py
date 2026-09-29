"""Restart one owned frontend and require a live clear event before each trial."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time
from urllib.request import Request, urlopen
from uuid import uuid4


CLEAR_METRIC = 'dynamo_component_kv_cache_events_applied'
REGISTERED_METRIC = 'dynamo_component_router_worker_registered'
MAIN_REVISION = 'f5d3353e2167bb0f0d729085eb5bc9183bf4b222'
CLEAR_SOURCES = {
    'lib/kv-router/src/scheduling/config.rs': '818c93fd1166b13ae86cc2b49bc18768870c222ce74419639fb3dc064805b65e',
    'lib/kv-router/src/indexer/concurrent_radix_tree_compressed/sync_impl.rs': '895eeec246a6e9f6a86efa59d73d31d16c433fca508b4e14d3b7061b69659212',
    'lib/kv-router/src/indexer/concurrent_radix_tree_compressed/mod.rs': 'd7ef74b2dc4a31e9a2086616400c211c4ee11de53644899f84791ae1aeff38e2',
    'lib/kv-router/src/indexer/concurrent_radix_tree_compressed/remove.rs': 'b8f163623f4a4137bee8da2902cdd2b740f2a96085caebe7d178c6fa682fe494',
}


def metric_rows(raw, name):
    rows = []
    label_pattern = r'([a-zA-Z_][a-zA-Z_0-9]*)=("(?:[^"\\]|\\.)*")(?:,|$)'
    for line in raw.decode().splitlines():
        if not line.startswith(name + '{'):
            continue
        match = re.fullmatch(re.escape(name) + r'\{(.*)\}\s+(\d+)(?:\.0+)?', line)
        if not match:
            raise ValueError('Invalid required frontend metric')
        labels, position = {}, 0
        for item in re.finditer(label_pattern, match[1]):
            if item.start() != position or item[1] in labels:
                raise ValueError('Invalid or duplicate metric labels')
            labels[item[1]] = json.loads(item[2])
            position = item.end()
        if position != len(match[1]):
            raise ValueError('Incomplete metric labels')
        rows.append((labels, int(match[2])))
    return rows


def clear_metrics_snapshot(raw):
    registered = metric_rows(raw, REGISTERED_METRIC)
    if any(value not in (0, 1) for _, value in registered):
        raise ValueError('Invalid worker registration gauge')
    active = [labels for labels, value in registered if value == 1]
    if len(active) != 1 or active[0].get('dp_rank') != '0':
        raise ValueError('Clear proof requires one registered backend at DP rank zero')
    registration = active[0]
    base = {name: registration.get(name) for name in ('dynamo_component', 'dynamo_namespace', 'worker_id')}
    if not all(base.values()) or not registration.get('router_worker_id'):
        raise ValueError('Worker registration lacks explicit identities')
    counters = {}
    for labels, value in metric_rows(raw, CLEAR_METRIC):
        if ({name: labels.get(name) for name in base} != base or
                set(labels) != set(base) | {'event_type', 'status'}):
            raise ValueError('Clear counters have an unexpected component identity')
        key = labels['event_type'], labels['status']
        if key in counters:
            raise ValueError('Duplicate clear counter series')
        counters[key] = value
    statuses = {'ok', 'allocation_failed', 'block_not_found', 'capacity_exhausted',
                'indexer_invariant_violation', 'invalid_block', 'ownership_degree_overflow',
                'parent_block_not_found', 'unsupported_residency_domain'}
    if set(counters) != {(event, status) for event in ('stored', 'removed', 'cleared') for status in statuses}:
        raise ValueError('Required clear counter series are missing or changed')
    return registration, counters


def clear_metrics_proof(before, after):
    first_worker, first = clear_metrics_snapshot(before)
    last_worker, last = clear_metrics_snapshot(after)
    if first_worker != last_worker:
        raise ValueError('Registered worker changed during clear')
    delta = {key: last[key] - first[key] for key in first}
    if any(value < 0 for value in delta.values()):
        raise ValueError('Frontend counters reset during clear')
    if any(value != 0 for (event, status), value in delta.items() if status != 'ok' or event == 'stored'):
        raise ValueError('Stored events or indexer errors occurred during clear')
    if delta['cleared', 'ok'] != 1:
        raise ValueError('Clear proof requires exactly one successful clear')
    return {'method': 'concurrent_indexer_applied_clear_counter', 'registration': first_worker,
            'cleared_ok_delta': 1, 'stored_delta': 0, 'error_delta': 0,
            'removed_ok_delta': delta['removed', 'ok'], 'source_revision': MAIN_REVISION,
            'source_sha256': CLEAR_SOURCES,
            'scope': 'Zero reusable indexed coverage for the sole registered worker/rank; physical tree-node count is unmeasured.'}


def read_frontend_metrics():
    with urlopen('http://127.0.0.1:8000/metrics', timeout=3) as response:
        raw = response.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError('Frontend metrics exceeded capture bound')
    return raw


def require_concurrent_main(state, study_dir, variant):
    require_owned_frontend(state)
    pid = state['frontend_pid']
    argv = [item.decode() for item in Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0') if item]
    expected = ['-m', 'dynamo.frontend', '--discovery-backend', 'file', '--request-plane', 'tcp',
                '--router-mode', 'kv', '--http-host', '127.0.0.1', '--http-port', '8000', '--enable-anthropic-api']
    if argv != [str(study_dir / f'frontend-{variant}/bin/python'), *expected]:
        raise ValueError('Metrics proof requires the matched-main study frontend command')
    env = dict(item.split(b'=', 1) for item in Path(f'/proc/{pid}/environ').read_bytes().split(b'\0') if b'=' in item)
    if any(name.startswith((b'DYN_ROUTER_', b'DYN_SHARED_CACHE_')) or name == b'DYN_USE_REMOTE_INDEXER' for name in env):
        raise ValueError('Metrics proof requires default concurrent router configuration without overrides')
    manifest_path = Path(__file__).resolve().parents[5] / 'wheels/artifact-manifest.json'
    manifest = json.loads(manifest_path.read_text())
    if manifest['revision'] != MAIN_REVISION:
        raise ValueError('Metrics proof source revision differs from the installed study artifacts')
    return {'router_event_threads': 4, 'configuration': 'pinned defaults, exact argv, no router overrides',
            'artifact_manifest_sha256': hashlib.sha256(manifest_path.read_bytes()).hexdigest()}


def require_raw_clear(study_dir, since):
    current = json.loads((study_dir / 'phase-active.json').read_text())
    if current.get('phase') != 'prefill' or current.get('status') != 'ready_for_live_gates':
        raise ValueError('Raw clear proof requires the active prefill epoch')
    kv = Path(current['kv_directory'])
    config = json.loads((kv / 'config.json').read_text())
    if config.get('hostname') != socket.gethostname() or config.get('operator_supplied_worker_epoch') != current['epoch']:
        raise ValueError('KV collector identity differs from active prefill')
    readiness = Path(__file__).resolve().parent.parent / 'gpu-readiness'
    sys.path.insert(0, str(readiness))
    from trial import wait_clear
    clear = wait_clear(kv / 'frames.jsonl', since, config['topic'])
    return clear


def start_ticks(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return None if fields[0] == 'Z' else int(fields[19])
    except FileNotFoundError:
        return None


def require_owned_frontend(state):
    pid, expected = state['frontend_pid'], state['frontend_start_ticks']
    if type(pid) is not int or pid <= 1 or type(expected) is not int or expected < 0:
        raise ValueError('Invalid recorded frontend identity')
    if start_ticks(pid) != expected:
        raise ValueError('Recorded frontend process has disappeared or changed')
    arguments = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
    if not any(arguments[i:i+2] == [b'-m', b'dynamo.frontend'] for i in range(len(arguments)-1)):
        raise ValueError('Recorded PID is not a Dynamo frontend process')
    return pid, expected


def terminate_owned(pid, ticks, grace=8):
    if start_ticks(pid) != ticks:
        return
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        # The descriptor keeps signals tied to this process if its PID is reused.
        if start_ticks(pid) != ticks:
            return
        try:
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                if start_ticks(pid) != ticks:
                    return
                time.sleep(0.1)
            if start_ticks(pid) == ticks:
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 2
        while start_ticks(pid) == ticks:
            if time.monotonic() >= deadline:
                raise RuntimeError('Owned frontend did not stop')
            time.sleep(0.1)
    finally:
        os.close(descriptor)


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def reset_worker(args, record_dir):
    env = os.environ.copy()
    for name in ('DYN_TCP_RPC_PORT', 'DYN_TCP_RESPONSE_STREAM_PORT'):
        env.pop(name, None)
    env.update(DYN_LOG='error', DYN_SYSTEM_PORT='-1', DYN_SELF_HOST_METADATA='0',
               DYN_FILE_KV=str(args.study_dir / 'prefill/discovery'),
               DYN_DISCOVERY_BACKEND='file', DYN_REQUEST_PLANE='tcp',
               DYN_TCP_RESPONSE_STREAM_HOST='lo')
    command = [args.worker_python, str(args.reset_script), '--endpoint', args.reset_endpoint,
               '--timeout', '8', '--execute']
    result = subprocess.run(command, env=env, capture_output=True, timeout=10, check=False)
    (record_dir/'reset.stdout').write_bytes(result.stdout)
    (record_dir/'reset.stderr').write_bytes(result.stderr)
    if result.returncode:
        raise RuntimeError(f'Worker reset exited {result.returncode}; see saved stderr')
    report = json.loads(result.stdout)
    if report.get('backend_reset_reported') is not True:
        raise ValueError('Worker did not acknowledge the reset')
    return report


def request_json(url, payload=None, timeout=3):
    data = None if payload is None else json.dumps(payload).encode()
    request = Request(url, data=data, headers={'Content-Type':'application/json'})
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def clear_proof(log_path, since):
    for line in log_path.read_text(errors='replace').splitlines():
        try:
            record = json.loads(line)
            stamp = datetime.fromisoformat(record['time'].replace('Z', '+00:00')).timestamp()
        except (ValueError, KeyError, TypeError):
            continue
        if stamp < since or record.get('target') not in {'dynamo_kv_router::indexer::kv_indexer', 'dynamo_llm::kv_router::indexer::kv_indexer'}:
            continue
        message = record.get('message', '')
        if (re.search(r'\bevent_type=cleared\b', message)
                and re.search(r'\bsuccess=true\b', message)
                and re.search(r'\bglobal_radix_tree_size=0\b', message)):
            return record
    return None


def run(args):
    if not args.execute:
        return {'execute':False, 'note':'No process changed and no reset sent.'}
    if not sys.platform.startswith('linux'):
        raise ValueError('This hook requires Linux /proc identity checks')
    proof_mode = getattr(args, 'router_clear_proof', 'trace')
    args.study_dir = args.study_dir.resolve()
    if not args.reset_script.is_file():
        raise ValueError('Reset helper does not exist')
    if not args.reset_only:
        if args.state is None or not args.state.is_file() or args.launch_script is None or not args.launch_script.is_file():
            raise ValueError('Existing ownership state and launch script are required')
        if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
            raise ValueError('Safe restart requires Linux pidfd support')
    epoch = uuid4().hex
    record_dir = args.study_dir / 'prefill/proofs' / epoch
    record_dir.mkdir(parents=True, exist_ok=False)
    if args.reset_only:
        return reset_worker(args, record_dir)
    previous = json.loads(args.state.read_text())
    pid, ticks = require_owned_frontend(previous)
    previous_epoch = previous['frontend_epoch']
    if not isinstance(previous_epoch, str) or not previous_epoch:
        raise ValueError('Missing previous frontend epoch')
    terminate_owned(pid, ticks)
    variant = 'fixed' if args.condition == 'fixed' else 'stock'
    stdout_path = record_dir/'frontend.log'
    started = time.time()
    command = ['bash', str(args.launch_script), str(args.study_dir), 'prefill', variant, epoch]
    with stdout_path.open('wb') as output:
        child = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
    child_ticks = start_ticks(child.pid)
    if child_ticks is None:
        raise RuntimeError('Fresh frontend exited at launch')
    state = {'frontend_pid':child.pid, 'frontend_start_ticks':child_ticks,
             'frontend_epoch':epoch, 'previous_frontend_epoch':previous_epoch,
             'started_unix':started, 'condition':args.condition,
             'stdout_path':str(stdout_path), 'status':'starting'}
    write_json(args.state, state)
    try:
        deadline = time.monotonic() + 15
        while True:
            if child.poll() is not None:
                raise RuntimeError('Fresh frontend exited before readiness')
            try:
                models = request_json(args.base_url+'/models', timeout=1)
                if any(m.get('id') == 'Qwen/Qwen3-8B' for m in models.get('data', [])):
                    break
            except (OSError, ValueError):
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError('Fresh frontend model discovery timed out')
            time.sleep(0.2)
        require_owned_frontend(state)
        configuration = None
        if proof_mode == 'metrics':
            configuration = require_concurrent_main(state, args.study_dir, variant)
            metrics_before = read_frontend_metrics()
            clear_metrics_snapshot(metrics_before)
            (record_dir/'frontend-before.metrics.txt').write_bytes(metrics_before)
        reset_started = time.time()
        reset_report = reset_worker(args, record_dir)
        canary = request_json(args.base_url+'/completions', {
            'model':'Qwen/Qwen3-8B', 'prompt':[42], 'max_tokens':1,
            'temperature':0, 'stream':False}, timeout=8)
        write_json(record_dir/'canary.json', canary)
        raw_clear = None
        if proof_mode == 'metrics':
            if canary.get('usage', {}).get('prompt_tokens_details', {}).get('cached_tokens') != 0:
                raise ValueError('Metrics clear proof requires explicit cold canary usage')
            raw_clear = require_raw_clear(args.study_dir, reset_started)
        deadline = time.monotonic() + 5
        while True:
            if proof_mode == 'metrics':
                metrics_after = read_frontend_metrics()
                (record_dir/'frontend-after.metrics.txt').write_bytes(metrics_after)
                try:
                    proof = clear_metrics_proof(metrics_before, metrics_after)
                except ValueError as error:
                    if str(error) != 'Clear proof requires exactly one successful clear':
                        raise
                    _, counters_before = clear_metrics_snapshot(metrics_before)
                    _, counters_after = clear_metrics_snapshot(metrics_after)
                    if counters_after['cleared', 'ok'] != counters_before['cleared', 'ok']:
                        raise
                    proof = None
            else:
                proof = clear_proof(stdout_path, reset_started)
            if proof is not None:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError('No live successful clear with verified empty router coverage')
            time.sleep(0.1)
        require_owned_frontend(state)
        if proof_mode == 'metrics' and require_concurrent_main(state, args.study_dir, variant) != configuration:
            raise ValueError('Frontend configuration changed during metrics proof')
        proof_path = record_dir/'router-clear-proof.json'
        evidence = {'frontend':state, 'reset_started_unix':reset_started,
                               'reset_report':reset_report, 'proof_mode':proof_mode,
                               'stdout_sha256':hashlib.sha256(stdout_path.read_bytes()).hexdigest(),
                               'canary_sha256':hashlib.sha256((record_dir/'canary.json').read_bytes()).hexdigest()}
        if proof_mode == 'metrics':
            evidence.update(metrics_proof=proof, configuration=configuration, raw_clear=raw_clear,
                            metrics_before_sha256=hashlib.sha256(metrics_before).hexdigest(),
                            metrics_after_sha256=hashlib.sha256(metrics_after).hexdigest())
        else:
            evidence['trace_record'] = proof
        write_json(proof_path, evidence)
        state.update(status='ready', router_index_empty=True, proof_file=str(proof_path),
                     proof_sha256=hashlib.sha256(proof_path.read_bytes()).hexdigest())
        write_json(args.state, state)
        return state
    except Exception:
        terminate_owned(child.pid, child_ticks, grace=1)
        state['status'] = 'failed'
        write_json(args.state, state)
        raise


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--study-dir', type=Path, required=True)
    result.add_argument('--state', type=Path)
    result.add_argument('--condition', choices=('off','stock','fixed'), default='stock')
    result.add_argument('--launch-script', type=Path)
    result.add_argument('--reset-script', type=Path, required=True)
    result.add_argument('--reset-endpoint', default='dynamo.backend.clear_kv_blocks')
    result.add_argument('--worker-python', default=sys.executable)
    result.add_argument('--base-url', default='http://127.0.0.1:8000/v1')
    result.add_argument('--reset-only', action='store_true')
    result.add_argument('--router-clear-proof', choices=('trace', 'metrics'), default='trace')
    result.add_argument('--execute', action='store_true')
    return result

if __name__ == '__main__':
    args = parser().parse_args()
    if args.execute and not args.reset_only and (args.state is None or args.launch_script is None):
        raise SystemExit('--state and --launch-script are required for a frontend restart')
    if args.base_url != 'http://127.0.0.1:8000/v1':
        raise SystemExit('This frozen hook only accepts the loopback study endpoint')
    print(json.dumps(run(args)))
