"""Switch owned study processes and run the frozen prefill schedule."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from urllib.request import urlopen
from uuid import uuid4

from prepare import ROOT, REPO, json_bytes, sha


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LAUNCH = ROOT.parent / 'linux-readiness/launch-on-pod.sh'
HOOK_PATH = LAUNCH.parent / 'fresh_frontend.py'
hook = load_module('phase_frontend_hook', HOOK_PATH)
identity = load_module('phase_metrics_identity', REPO / 'metrics_identity.py')
UPSTREAM = ROOT.parents[1]


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    hook.write_json(path, value)


def active(args):
    path = args.study_dir / 'phase-active.json'
    if path.exists():
        return json.loads(path.read_text())
    folder = args.study_dir / 'parity'
    return {'phase': 'parity', 'directory': str(folder), 'processes_file': str(folder / 'processes.json'),
            'frontend_state': str(folder / 'frontend-state.json'), 'epoch': 'bootstrap', 'status': 'running'}


def stop(args):
    current = active(args)
    states_path = Path(current['processes_file'])
    states = json.loads(states_path.read_text())
    directory = Path(current['directory'])
    front_path = Path(current['frontend_state'])
    if front_path.exists():
        front = json.loads(front_path.read_text())
        states.pop('image', None)
        states['frontend'] = {'pid': front['frontend_pid'], 'start_ticks': front['frontend_start_ticks']}
        save(directory / 'frontend-state-at-stop.json', front)
    backend = states.get('backend')
    descendants = []
    if backend and hook.start_ticks(backend['pid']) == backend['start_ticks']:
        descendants = identity.process_tree(Path('/proc'), backend['pid'])
    save(directory / 'processes-at-stop.json', {'recorded': states, 'backend_tree': descendants})
    for name in ('frontend', 'image', 'identity', 'backend'):
        if name in states:
            row = states[name]
            hook.terminate_owned(row['pid'], row['start_ticks'])
    for row in reversed(descendants):
        hook.terminate_owned(row['pid'], row['start_ticks'], grace=2)
    for name in ('kv', 'capture'):
        if name in states:
            row = states[name]
            hook.terminate_owned(row['pid'], row['start_ticks'], grace=4)
    capture = states.get('capture')
    if capture:
        raw = Path(capture['stdout_path']).read_text()
        count = re.search(r'(\d+) packets captured', raw)
        drops = re.search(r'(\d+) packets dropped by kernel', raw)
        save(directory / 'capture-final.json', {'packets_captured': int(count[1]) if count else None,
            'packets_dropped': int(drops[1]) if drops else None,
            'zero_drops_verified': bool(count and int(count[1]) > 0 and drops and int(drops[1]) == 0)})
    current.update(status='stopped', stopped_unix=time.time())
    save(args.study_dir / 'phase-active.json', current)
    return current


def start(args):
    if active(args).get('status') != 'stopped':
        stop(args)
    epoch = uuid4().hex
    folder = args.study_dir / args.phase / 'epochs' / epoch
    folder.mkdir(parents=True, exist_ok=False)
    discovery = args.study_dir / args.phase / 'discovery'
    if discovery.exists():
        discovery.rename(folder / 'previous-discovery')
    environment = os.environ.copy()
    environment['PATH'] = str(Path(args.backend_python).parent) + os.pathsep + environment.get('PATH', '')
    states = {}
    state_path = folder / 'processes.json'
    save(state_path, states)
    front_path = args.study_dir / args.phase / 'frontend-state.json'
    record = {'phase': args.phase, 'epoch': epoch, 'directory': str(folder),
              'processes_file': str(state_path), 'frontend_state': str(front_path), 'status': 'starting'}
    save(args.study_dir / 'phase-active.json', record)

    def launch(name, argv):
        stdout = folder / (name + '.log')
        with stdout.open('wb') as output:
            child = subprocess.Popen(argv, stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     start_new_session=True, env=environment)
        ticks = hook.start_ticks(child.pid)
        if ticks is None:
            raise RuntimeError(name + ' exited immediately')
        states[name] = {'pid': child.pid, 'start_ticks': ticks, 'epoch': epoch, 'component': name,
                        'started_unix': time.time(), 'stdout_path': str(stdout), 'command': argv}
        save(state_path, states)
        return states[name]

    try:
        launch('capture', [shutil.which('tcpdump') or 'tcpdump', '--immediate-mode', '-i', 'lo', '-s', '0',
            '-U', '-B', '4096', '-w', str(folder / 'wire.pcap'),
            'tcp', 'and', '(', 'port', '20000', 'or', 'port', '20003', ')'])
        launch('kv', [str(args.control_python), str(UPSTREAM / 'gpu-readiness/collect_kv.py'),
                     '--worker-epoch', epoch, '--output', str(folder / 'kv')])
        deadline = time.monotonic() + 8
        while 'listening on lo' not in (folder / 'capture.log').read_text() or not (folder / 'kv/frames.jsonl').exists():
            if time.monotonic() >= deadline:
                raise TimeoutError('Phase observers did not become ready')
            time.sleep(.1)
        backend = launch('backend', ['bash', str(LAUNCH), str(args.study_dir), args.phase, 'backend', epoch])
        frontend = launch('frontend', ['bash', str(LAUNCH), str(args.study_dir), args.phase,
                                      'stock' if args.phase == 'prefill' else 'image', epoch])
        save(front_path, {'frontend_pid': frontend['pid'], 'frontend_start_ticks': frontend['start_ticks'],
            'frontend_epoch': epoch, 'started_unix': frontend['started_unix'],
            'stdout_path': frontend['stdout_path'], 'status': 'starting'})
        deadline = time.monotonic() + args.wait_seconds
        while True:
            if any(hook.start_ticks(row['pid']) != row['start_ticks'] for row in states.values()):
                raise RuntimeError('An owned phase process exited during startup')
            try:
                with urlopen('http://127.0.0.1:8000/v1/models', timeout=2) as response:
                    models = json.load(response)
                if any(row.get('id') == 'Qwen/Qwen3-8B' for row in models.get('data', [])):
                    break
            except (OSError, ValueError):
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError('Phase model discovery timed out')
            time.sleep(.5)
        while True:
            if any(hook.start_ticks(row['pid']) != row['start_ticks'] for row in states.values()):
                raise RuntimeError('An owned phase process exited before engine readiness')
            tree = identity.process_tree(Path('/proc'), backend['pid'])
            evidence = []
            for row in tree:
                proc = Path('/proc') / str(row['pid'])
                try:
                    evidence.append({**row, 'comm': (proc / 'comm').read_text().strip(),
                        'cmdline': [value.decode(errors='replace') for value in (proc / 'cmdline').read_bytes().split(b'\0') if value]})
                except FileNotFoundError:
                    continue
            engines = [row['pid'] for row in evidence if row['pid'] != backend['pid'] and
                       (row['comm'].startswith('VLLM::EngineCor') or (row['cmdline'] and row['cmdline'][0].startswith('VLLM::EngineCore')))]
            save(folder / 'backend-process-tree.json', evidence)
            if len(engines) > 1:
                raise ValueError('Expected exactly one named TP1 VLLM::EngineCore descendant')
            try:
                if len(engines) != 1:
                    raise identity.IdentityUnavailable('Engine process is not visible yet')
                identity.ProcessMonitor(backend['pid'], engines, 8081)
                break
            except (identity.IdentityUnavailable, OSError):
                if time.monotonic() >= deadline:
                    raise TimeoutError('Engine identity or metrics listener did not become ready')
                time.sleep(.5)
        launch('identity', [str(args.control_python), str(REPO / 'metrics_identity.py'),
            '--backend-pid', str(backend['pid']), '--engine-pid', str(engines[0]), '--metrics-port', '8081', '--port', '9099'])
        deadline = time.monotonic() + 5
        while True:
            challenge = uuid4().hex
            try:
                with urlopen('http://127.0.0.1:9099/identity?challenge=' + challenge, timeout=1) as response:
                    value = json.load(response)
                fingerprint = identity.validate_identity(value, challenge)
                break
            except (OSError, ValueError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.1)
        save(folder / 'identity-start.json', value)
        record.update(status='ready_for_live_gates', identity_fingerprint=fingerprint,
                      kv_directory=str(folder / 'kv'), wire_pcap=str(folder / 'wire.pcap'))
        save(args.study_dir / 'phase-active.json', record)
        return record
    except Exception:
        stop(args)
        raise


def command(args, argv, directory, name):
    remaining = args.stop_at_unix - time.time()
    if remaining < 5:
        raise TimeoutError('Phase admission deadline reached')
    with (directory / (name + '.stdout')).open('wb') as out, (directory / (name + '.stderr')).open('wb') as err:
        child = subprocess.Popen(argv, stdout=out, stderr=err, stdin=subprocess.DEVNULL, start_new_session=True)
        ticks = hook.start_ticks(child.pid)
        save(directory / (name + '.command.json'), {'argv': argv, 'pid': child.pid, 'start_ticks': ticks,
                                                   'started_unix': time.time(), 'deadline_unix': args.stop_at_unix})
        try:
            result = child.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            descendants = identity.process_tree(Path('/proc'), child.pid) if hook.start_ticks(child.pid) == ticks else []
            for row in reversed(descendants):
                hook.terminate_owned(row['pid'], row['start_ticks'], grace=1)
            raise
    if result:
        raise RuntimeError(name + ' failed; inspect saved output')


def wire_snapshot(args, current, name):
    folder = Path(current['directory'])
    snapshot = folder / (name + '.pcap')
    # Idle transport can still be flushing its final sentinel into the PCAP.
    time.sleep(.25)
    snapshot.write_bytes(Path(current['wire_pcap']).read_bytes())
    logs = sorted({Path(json.loads(Path(current['frontend_state']).read_text())['stdout_path']),
                   *[p for p in (args.study_dir / 'prefill/proofs').glob('*/frontend.log')],
                   folder / 'frontend.log'})
    argv = [str(args.control_python), str(UPSTREAM / 'parity-run-1/wire-capture/decode.py'),
            str(snapshot), '--output', str(folder / (name + '-wire.json'))]
    for address in getattr(args, 'local_address', []):
        argv += ['--local-address', address]
    for path in logs:
        argv += ['--frontend-log', str(path)]
    command(args, argv, folder, name + '-decode')
    return folder / (name + '-wire.json')


def kv_capture_proof(current):
    from kv_report import collector, events
    directory = Path(current['kv_directory'])
    config = json.loads((directory / 'config.json').read_text())
    if config.get('operator_supplied_worker_epoch') != current['epoch']:
        raise ValueError('KV collector epoch differs from the active worker')
    ledger = collector.PublishedBlocks()
    saw_clear = False
    for row in events(directory / 'frames.jsonl', allow_partial=True):
        if row['topic'] != config['topic']:
            raise ValueError('Unexpected KV event topic')
        state = ledger.accept(row['sequence'], row['payload_sha256'], row['batch'])
        saw_clear |= bool(state.get('clear_events'))
    if not saw_clear or not ledger.complete_since_clear or ledger.issues or not ledger.blocks:
        raise ValueError('No complete observed clear-to-stored KV sequence from the smoke')
    return {'config_sha256': sha((directory / 'config.json').read_bytes()),
            'last_sequence': ledger.last_sequence, 'published_entries': len(ledger.blocks),
            'complete_since_clear': True}


def validate_repin(previous, observed):
    for key in ('boot_id', 'backend_pid', 'engine_pids', 'metrics_port', 'metrics_socket_inodes'):
        if previous[key] != observed[key]:
            raise ValueError('Identity rebind changed ' + key)
    before = {row['pid']: row for row in previous['processes']}
    after = {row['pid']: row for row in observed['processes']}
    if set(before) != set(after):
        raise ValueError('Identity rebind changed process membership')
    for pid, row in before.items():
        expected = dict(row)
        if pid == previous['backend_pid'] and after[pid]['parent_pid'] != row['parent_pid']:
            if after[pid]['parent_pid'] != 1:
                raise ValueError('Backend was not reparented to PID 1')
            expected['parent_pid'] = 1
        if after[pid] != expected:
            raise ValueError('Identity rebind changed a process start time or engine relationship')


def repin_identity(args, current):
    folder = Path(current['directory'])
    previous_path = folder / 'identity-start.json'
    previous = json.loads(previous_path.read_text())
    # The switch command has exited; its surviving backend is now owned by PID 1.
    monitor = identity.ProcessMonitor(previous['backend_pid'], previous['engine_pids'], previous['metrics_port'])
    observed = monitor.snapshot()
    validate_repin(previous, observed)
    states_path = Path(current['processes_file'])
    states = json.loads(states_path.read_text())
    old = states['identity']
    if hook.start_ticks(old['pid']) != old['start_ticks']:
        raise ValueError('Recorded identity sidecar is no longer the owned process')
    evidence = {'reason': 'Rebind after the switch launcher exits, before measured trials',
                'previous_identity_sha256': sha(previous_path.read_bytes()),
                'previous_identity': previous, 'observed_before_rebind': observed,
                'previous_sidecar': old, 'started_unix': time.time(), 'status': 'rebind_started'}
    save(folder / 'identity-repin.json', evidence)
    hook.terminate_owned(old['pid'], old['start_ticks'])
    argv = [str(args.control_python), str(REPO / 'metrics_identity.py'),
            '--backend-pid', str(previous['backend_pid']), '--metrics-port', str(previous['metrics_port']), '--port', '9099']
    for pid in previous['engine_pids']:
        argv += ['--engine-pid', str(pid)]
    stdout = folder / 'identity-measurement.log'
    with stdout.open('wb') as output:
        child = subprocess.Popen(argv, stdout=output, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
    ticks = hook.start_ticks(child.pid)
    if ticks is None:
        raise RuntimeError('Replacement identity sidecar exited immediately')
    states['identity'] = {'pid': child.pid, 'start_ticks': ticks, 'epoch': current['epoch'],
                          'component': 'identity', 'started_unix': time.time(),
                          'stdout_path': str(stdout), 'command': argv}
    save(states_path, states)
    deadline = time.monotonic() + 5
    while True:
        challenge = uuid4().hex
        try:
            with urlopen('http://127.0.0.1:9099/identity?challenge=' + challenge, timeout=1) as response:
                value = json.load(response)
            fingerprint = identity.validate_identity(value, challenge)
            if value['monitor_id'] == previous['monitor_id']:
                raise ValueError('Identity sidecar was not replaced')
            validate_repin(previous, value)
            if value['processes'] != observed['processes']:
                raise ValueError('Process tree changed during identity rebind')
            break
        except (OSError, ValueError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(.1)
    save(folder / 'identity-measurement-start.json', value)
    evidence.update(status='rebound_before_measurement', replacement_identity=value,
                    replacement_sidecar=states['identity'], ended_unix=time.time())
    save(folder / 'identity-repin.json', evidence)
    current['identity_fingerprint'] = fingerprint
    current['identity_measurement_start'] = str(folder / 'identity-measurement-start.json')
    save(args.study_dir / 'phase-active.json', current)


def prefill(args):
    current = active(args)
    if current['phase'] != 'prefill' or current.get('status') != 'ready_for_live_gates':
        raise ValueError('Switch to the prefill phase first')
    folder = Path(current['directory'])
    if time.time() >= args.stop_at_unix:
        raise TimeoutError('Prefill admission is closed')
    common = [str(args.control_python), str(HOOK_PATH), '--study-dir', str(args.study_dir),
        '--reset-script', str(ROOT / 'reset_worker.py'), '--worker-python', args.backend_python,
        '--router-clear-proof', getattr(args, 'router_clear_proof', 'trace')]
    hooks = {'worker_reset': common + ['--reset-only', '--execute'],
             'fresh_frontend': common + ['--state', current['frontend_state'], '--condition', '{condition}',
                                       '--launch-script', str(LAUNCH), '--execute']}
    hooks_path = folder / 'hooks.json'
    save(hooks_path, hooks)
    manifest = {'status': 'smoke_started', 'epoch': current['epoch'], 'completed_trials': [],
                'stop_at_unix': args.stop_at_unix}
    try:
        repin_identity(args, current)
        for variant in ('stock', 'fixed'):
            argv = [arg.replace('{condition}', variant) for arg in hooks['fresh_frontend']]
            command(args, argv, folder, variant + '-smoke-reset')
            command(args, [str(args.control_python), str(ROOT / 'live_hint_smoke.py'), '--variant', variant,
                '--output', str(folder / ('smoke-' + variant)), '--execute'], folder, variant + '-smoke')
            wire = wire_snapshot(args, current, variant + '-smoke')
            command(args, [str(args.control_python), str(ROOT / 'live_hint_smoke.py'),
                '--output', str(folder / ('smoke-' + variant)), '--check-wire', str(wire)], folder, variant + '-warmup-check')
        kv_evidence = kv_capture_proof(current)
        proof = {'token_capture_ready': True, 'kv_event_capture_ready': True, 'worker_epoch': current['epoch'],
                 'token_evidence': [str(folder / ('smoke-' + variant) / 'warmup-wire-check.json') for variant in ('stock', 'fixed')],
                 'kv_evidence': kv_evidence}
        proof_path = folder / 'capture-proof.json'
        save(proof_path, proof)
        manifest['status'] = 'trials_started'
        save(folder / 'prefill-batch.json', manifest)
        for row in json.loads((ROOT / 'fixtures/schedule.json').read_text()):
            if args.stop_at_unix - time.time() < 60:
                manifest['status'] = 'phase_time_limit'
                break
            name = f"{row['session']}-{row['condition']}-r{row['repeat']}"
            command(args, [str(args.control_python), str(ROOT / 'trial.py'), '--session', row['session'],
                '--condition', row['condition'], '--repeat', str(row['repeat']), '--hooks', str(hooks_path),
                '--capture-proof', str(proof_path), '--kv-directory', current['kv_directory'],
                '--output', str(folder / 'trials' / name), '--execute'], folder, name)
            manifest['completed_trials'].append(name)
            save(folder / 'prefill-batch.json', manifest)
        else:
            manifest['status'] = 'all_trials_completed'
    except Exception as error:
        manifest.update(status='failed', error_type=type(error).__name__, error=str(error))
        raise
    finally:
        try:
            manifest['phase_stop'] = stop(args)
        except Exception as cleanup_error:
            manifest['cleanup_error'] = str(cleanup_error)
        manifest['ended_unix'] = time.time()
        save(folder / 'prefill-batch.json', manifest)
    if manifest.get('cleanup_error'):
        raise RuntimeError('Prefill cleanup failed; inspect prefill-batch.json')
    return manifest


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('switch', 'prefill', 'stop'))
    p.add_argument('--phase', choices=('parity', 'prefill'))
    p.add_argument('--study-dir', type=Path, default=Path('/tmp/reedcode-study'))
    p.add_argument('--control-python', type=Path, default=Path('/tmp/reedcode-observer-venv/bin/python'))
    p.add_argument('--backend-python')
    p.add_argument('--wait-seconds', type=int, default=240)
    p.add_argument('--stop-at-unix', type=float)
    p.add_argument('--router-clear-proof', choices=('trace', 'metrics'), default='trace')
    p.add_argument('--local-address', action='append', default=[],
                   help='Observed same-host address allowed by the passive wire decoder')
    p.add_argument('--execute', action='store_true')
    args = p.parse_args()
    if args.action == 'switch' and args.phase is None:
        p.error('switch requires --phase')
    if args.action == 'prefill' and args.stop_at_unix is None:
        p.error('prefill requires the approved absolute phase deadline via --stop-at-unix')
    if not args.execute:
        print(json.dumps({'execute': False, 'action': args.action, 'phase': args.phase,
                          'study_dir': str(args.study_dir), 'stop_at_unix': args.stop_at_unix}, indent=2))
        return
    if not sys.platform.startswith('linux'):
        p.error('Execution requires the approved Linux study container')
    if args.backend_python is None:
        args.backend_python = json.loads((args.study_dir / 'bootstrap/identity.json').read_text())['python']
    if not Path(args.backend_python).is_absolute() or not args.control_python.is_absolute():
        p.error('Interpreter paths must be absolute')
    print(json.dumps({'switch': start, 'prefill': prefill, 'stop': stop}[args.action](args), indent=2))


if __name__ == '__main__':
    main()
