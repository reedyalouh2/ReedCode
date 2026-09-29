"""Prove an empty stock-parity cache and router index before one real client."""

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import Request, urlopen
from uuid import uuid4


UPSTREAM = Path(__file__).resolve().parents[2]
PREFILL = UPSTREAM / '001-speculative-prefill/gpu-readiness'
sys.path.insert(0, str(PREFILL))
from phase_control import active, hook, identity
from prepare import json_bytes, sha
from kv_report import metrics_reader
from replay import send
from trial import wait_clear


def reset_environment(study_dir, inherited):
    env = dict(inherited)
    for name in ('DYN_TCP_RPC_PORT', 'DYN_TCP_RESPONSE_STREAM_PORT'):
        env.pop(name, None)
    env.update(DYN_FILE_KV=str(study_dir / 'parity/discovery'), DYN_DISCOVERY_BACKEND='file',
               DYN_REQUEST_PLANE='tcp', DYN_TCP_RESPONSE_STREAM_HOST='lo', DYN_LOG='error',
               DYN_SYSTEM_PORT='-1', DYN_SELF_HOST_METADATA='0')
    return env


def canary_payload():
    return {'model': 'Qwen/Qwen3-8B', 'prompt': [42], 'max_tokens': 1,
            'temperature': 0, 'stream': True, 'stream_options': {'include_usage': True}}


def require_idle(raw):
    metrics = metrics_reader.parse_metrics(raw.decode(), 'Qwen/Qwen3-8B')
    for metric in ('vllm:num_requests_running', 'vllm:num_requests_waiting'):
        series = metrics.get(metric, {})
        if not series or any(dict(labels).get('engine') != '0' or value != 0 for labels, value in series.items()):
            raise ValueError('Parity reset needs an idle, single-engine backend')


def run(args):
    clean_start = getattr(args, 'clean_start_proof', 'frontend-trace')
    if clean_start not in ('frontend-trace', 'first-request'):
        raise ValueError('Unknown clean-start proof mode')
    if not args.execute:
        return {'execute': False, 'discovery': str(args.study_dir / 'parity/discovery'),
                'canary': canary_payload(), 'clean_start_proof': clean_start,
                'proof': ('live clear event and current stock frontend tree size zero' if clean_start == 'frontend-trace'
                          else 'live reset and KV clear; first real request cached_tokens=0 remains required')}
    if not sys.platform.startswith('linux'):
        raise ValueError('Run only on the approved Linux study pod')
    current = active(args)
    if current['phase'] != 'parity' or current.get('status') == 'stopped':
        raise ValueError('The active phase must be the running stock parity server')
    folder = Path(current['directory'])
    states = json.loads(Path(current['processes_file']).read_text())
    backend = states['backend']
    if hook.start_ticks(backend['pid']) != backend['start_ticks']:
        raise ValueError('Owned parity backend has exited or changed')
    frontend = json.loads(Path(current['frontend_state']).read_text())
    hook.require_owned_frontend(frontend)
    kv_dir = Path(current.get('kv_directory', str(folder / 'kv')))
    config = json.loads((kv_dir / 'config.json').read_text())
    if (config.get('hostname') != socket.gethostname()
            or config.get('operator_supplied_worker_epoch') != backend['epoch']):
        raise ValueError('Parity collector hostname or worker epoch differs from the active backend')
    python = args.backend_python or json.loads((args.study_dir / 'bootstrap/identity.json').read_text())['python']
    if not Path(python).is_absolute():
        raise ValueError('Backend Python must be an absolute recorded path')
    nonce = uuid4().hex
    output = folder / 'reset-proofs' / nonce
    output.mkdir(parents=True, exist_ok=False)
    report = {'status': 'started', 'client': args.client, 'phase': 'parity', 'worker_epoch': backend['epoch'],
              'frontend_epoch': frontend['frontend_epoch'], 'output': str(output), 'started_unix': time.time(),
              'backend_cache_empty': False, 'router_index_empty': None if clean_start == 'first-request' else False,
              'clean_start_proof': clean_start}
    try:
        tree = identity.process_tree(Path('/proc'), backend['pid'])
        report['backend_process_tree_before'] = tree
        with urlopen(Request('http://127.0.0.1:8081/metrics', headers={'Cache-Control': 'no-cache'}), timeout=5) as response:
            metrics = response.read(4 * 1024 * 1024 + 1)
        (output / 'before.metrics.txt').write_bytes(metrics)
        if len(metrics) > 4 * 1024 * 1024:
            raise ValueError('Metrics capture exceeded its bound')
        require_idle(metrics)
        command = [python, str(PREFILL / 'reset_worker.py'), '--endpoint', 'dynamo.backend.clear_kv_blocks',
                   '--timeout', '8', '--execute']
        report['reset_command'] = command
        report['reset_started_unix'] = time.time()
        reset = subprocess.run(command, env=reset_environment(args.study_dir, os.environ), capture_output=True, timeout=10)
        (output / 'reset.stdout').write_bytes(reset.stdout)
        (output / 'reset.stderr').write_bytes(reset.stderr)
        if reset.returncode:
            raise RuntimeError('Parity worker reset failed; inspect saved output')
        reset_result = json.loads(reset.stdout)
        if reset_result.get('backend_reset_reported') is not True:
            raise ValueError('Parity worker did not acknowledge its reset')
        report['reset_response'] = reset_result
        request = canary_payload()
        (output / 'canary-request.json').write_bytes(json_bytes(request))
        canary, _ = send('http://127.0.0.1:8000/v1/completions', {'payload': request},
                         'parity-reset-' + nonce, output, 15)
        report['canary'] = canary
        if canary['cached_tokens'] != 0:
            raise ValueError('Parity reset canary unexpectedly reused tokens')
        clear = wait_clear(kv_dir / 'frames.jsonl', report['reset_started_unix'], config['topic'])
        log = Path(frontend['stdout_path'])
        proof = None
        if clean_start == 'frontend-trace':
            deadline = time.monotonic() + 5
            while True:
                proof = hook.clear_proof(log, report['reset_started_unix'])
                if proof is not None:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('No current parity frontend clear/size-zero trace')
                time.sleep(.1)
        hook.require_owned_frontend(frontend)
        if hook.start_ticks(backend['pid']) != backend['start_ticks']:
            raise ValueError('Parity backend changed during reset')
        after_tree = identity.process_tree(Path('/proc'), backend['pid'])
        if after_tree != tree:
            raise ValueError('Parity backend process tree changed during reset')
        (output / 'kv-frames.jsonl').write_bytes((kv_dir / 'frames.jsonl').read_bytes())
        (output / 'kv-config.json').write_bytes((kv_dir / 'config.json').read_bytes())
        if proof is not None:
            (output / 'frontend-clear-row.json').write_bytes(json_bytes(proof))
        report.update(status='reset_verified' if clean_start == 'frontend-trace' else 'reset_awaiting_first_request',
                      backend_cache_empty=True, router_index_empty=True if clean_start == 'frontend-trace' else None,
                      backend_process_tree_after=after_tree, clear_event=clear,
                      frontend_log=str(log), frontend_log_sha256=sha(log.read_bytes()),
                      scope='No published full blocks remain; the one-token canary has no complete 16-token block.')
        if clean_start == 'first-request':
            report['first_real_request_proof'] = {'status': 'pending', 'required_cached_tokens': 0,
                'requirement': 'Save the first real client request and its explicitly linked server usage showing cached_tokens=0. The canary does not satisfy this check.'}
    except Exception as error:
        if isinstance(error, subprocess.TimeoutExpired):
            (output / 'reset.stdout').write_bytes(error.stdout or b'')
            (output / 'reset.stderr').write_bytes(error.stderr or b'')
        report.update(status='failed', error_type=type(error).__name__, error=str(error))
        raise
    finally:
        report['ended_unix'] = time.time()
        report['files'] = {path.name: sha(path.read_bytes()) for path in sorted(output.iterdir()) if path.is_file()}
        (output / 'result.json').write_bytes(json_bytes(report))
    return report


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--study-dir', type=Path, default=Path('/tmp/reedcode-study'))
    p.add_argument('--backend-python')
    p.add_argument('--client', choices=('claude', 'codex'), required=True)
    p.add_argument('--clean-start-proof', choices=('frontend-trace', 'first-request'), default='frontend-trace')
    p.add_argument('--execute', action='store_true')
    args = p.parse_args()
    if not args.study_dir.is_absolute():
        p.error('study-dir must be absolute')
    print(json.dumps(run(args), indent=2))
