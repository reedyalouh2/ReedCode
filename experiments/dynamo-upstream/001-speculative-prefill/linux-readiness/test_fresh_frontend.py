"""Checks for the trial hook; no real process is stopped."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('fresh_frontend', Path(__file__).with_name('fresh_frontend.py'))
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


def trace(stamp=100, message=None, target='dynamo_kv_router::indexer::kv_indexer'):
    return {'time':datetime.fromtimestamp(stamp, timezone.utc).isoformat(), 'target':target,
            'message':message or 'Applied KV event to global radix tree: event_type=cleared, event_id=2, worker_id=1, success=true, global_radix_tree_size=0'}


def metrics(cleared=0, stored=0, error=0, worker='9', dp_rank='0'):
    base = 'dynamo_component="backend",dynamo_namespace="dynamo",worker_id="front"'
    lines = [f'{hook.REGISTERED_METRIC}{{{base},router_worker_id="{worker}",dp_rank="{dp_rank}",worker_type="decode"}} 1']
    statuses = ('ok', 'allocation_failed', 'block_not_found', 'capacity_exhausted',
                'indexer_invariant_violation', 'invalid_block', 'ownership_degree_overflow',
                'parent_block_not_found', 'unsupported_residency_domain')
    for event in ('stored', 'removed', 'cleared'):
        for status in statuses:
            value = (cleared if event == 'cleared' else stored if event == 'stored' else 0) if status == 'ok' else error
            lines.append(f'{hook.CLEAR_METRIC}{{{base},event_type="{event}",status="{status}"}} {value}')
    return ('\n'.join(lines)+'\n').encode()


class HookChecks(unittest.TestCase):
    def test_metrics_proves_coverage_without_claiming_tree_size(self):
        result = hook.clear_metrics_proof(metrics(), metrics(cleared=1))
        self.assertEqual(result['cleared_ok_delta'], 1)
        self.assertEqual(result['registration']['router_worker_id'], '9')
        self.assertEqual(result['source_revision'], hook.MAIN_REVISION)
        self.assertIn('physical tree-node count is unmeasured', result['scope'])

    def test_metrics_rejects_missing_or_duplicate_series_and_multiple_workers(self):
        good = metrics(cleared=1)
        for raw in (good.replace(next(row for row in good.splitlines(keepends=True) if b'event_type="stored",status="ok"' in row), b''),
                    good + good.splitlines(keepends=True)[1],
                    good + good.splitlines(keepends=True)[0].replace(b'worker_id="9"', b'worker_id="10"')):
            with self.assertRaises(ValueError):
                hook.clear_metrics_proof(metrics(), raw)

    def test_metrics_rejects_reset_stores_errors_or_changed_registration(self):
        for before, after in ((metrics(), metrics()), (metrics(), metrics(cleared=2)),
                              (metrics(cleared=2), metrics(cleared=1)),
                              (metrics(), metrics(cleared=1, stored=1)),
                              (metrics(), metrics(cleared=1, error=1)),
                              (metrics(), metrics(cleared=1, worker='10')),
                              (metrics(), metrics(cleared=1, dp_rank='1'))):
            with self.assertRaises(ValueError):
                hook.clear_metrics_proof(before, after)

    def test_metrics_rejects_single_thread_override(self):
        study = Path('/tmp/reedcode-study')
        argv = '\0'.join(['/tmp/reedcode-study/frontend-stock/bin/python', '-m', 'dynamo.frontend',
                         '--discovery-backend', 'file', '--request-plane', 'tcp', '--router-mode', 'kv',
                         '--http-host', '127.0.0.1', '--http-port', '8000', '--enable-anthropic-api']).encode()
        with patch.object(hook, 'require_owned_frontend'), patch.object(Path, 'read_bytes', side_effect=[argv, b'DYN_ROUTER_EVENT_THREADS=1\0']):
            with self.assertRaisesRegex(ValueError, 'default concurrent'):
                hook.require_concurrent_main({'frontend_pid': 123}, study, 'stock')

    def test_clear_requires_current_target_success_and_zero(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'frontend.log'
            for row in (trace(99), trace(target='other'),
                        trace(message=trace()['message'].replace('true','false')),
                        trace(message=trace()['message'].replace('size=0','size=10')),
                        trace(message=trace()['message'].replace('cleared','stored'))):
                path.write_text('unstructured noise\n'+json.dumps(row)+'\n')
                self.assertIsNone(hook.clear_proof(path,100))
            path.write_text(json.dumps(trace())+'\n')
            self.assertEqual(hook.clear_proof(path,100),trace())

    def test_ownership_rejects_reused_pid_and_other_program(self):
        state={'frontend_pid':123,'frontend_start_ticks':20}
        with patch.object(hook,'start_ticks',return_value=21):
            with self.assertRaisesRegex(ValueError,'disappeared or changed'):
                hook.require_owned_frontend(state)
        with patch.object(hook,'start_ticks',return_value=20), patch.object(Path,'read_bytes',return_value=b'python\0-m\0other\0'):
            with self.assertRaisesRegex(ValueError,'not a Dynamo frontend'):
                hook.require_owned_frontend(state)
        with patch.object(hook,'start_ticks',return_value=20), patch.object(Path,'read_bytes',return_value=b'python\0-m\0dynamo.frontend\0'):
            self.assertEqual(hook.require_owned_frontend(state),(123,20))

    def test_no_signal_if_identity_changes_before_pidfd_acquisition(self):
        with patch.object(hook,'start_ticks',side_effect=[20,21]), patch.object(os,'pidfd_open',return_value=7,create=True), patch.object(hook.signal,'pidfd_send_signal',create=True) as send, patch.object(os,'close') as close:
            hook.terminate_owned(123,20)
            send.assert_not_called()
            close.assert_called_once_with(7)

    def test_signal_uses_pidfd_and_stops_after_exit(self):
        with patch.object(hook,'start_ticks',side_effect=[20,20,None]), patch.object(os,'pidfd_open',return_value=7,create=True), patch.object(hook.signal,'pidfd_send_signal',create=True) as send, patch.object(os,'close') as close:
            hook.terminate_owned(123,20)
            send.assert_called_once_with(7,hook.signal.SIGTERM)
            close.assert_called_once_with(7)

    def test_reset_unsets_occupied_ports_and_preserves_report(self):
        with tempfile.TemporaryDirectory() as temp:
            args=argparse.Namespace(study_dir=Path(temp), worker_python='python3', reset_script=Path('reset.py'),reset_endpoint='dynamo.backend.clear_kv_blocks')
            report={'backend_reset_reported':True,'router_reset_verified':False}
            result=subprocess.CompletedProcess([],0,json.dumps(report).encode(),b'')
            with patch.dict(os.environ,{'DYN_TCP_RPC_PORT':'20002','DYN_TCP_RESPONSE_STREAM_PORT':'20003'}), patch.object(subprocess,'run',return_value=result) as run:
                self.assertEqual(hook.reset_worker(args,Path(temp)),report)
                env=run.call_args.kwargs['env']
                self.assertNotIn('DYN_TCP_RPC_PORT',env)
                self.assertNotIn('DYN_TCP_RESPONSE_STREAM_PORT',env)
                self.assertEqual(env['DYN_FILE_KV'],str(Path(temp)/'prefill/discovery'))

    def test_dry_run_has_no_side_effects(self):
        with patch.object(subprocess,'Popen') as launch, patch.object(hook,'terminate_owned') as stop:
            self.assertFalse(hook.run(argparse.Namespace(execute=False))['execute'])
            launch.assert_not_called(); stop.assert_not_called()

    def test_ready_hook_emits_hash_bound_proof_and_unique_epoch(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            state_path=root/'state.json'
            state_path.write_text(json.dumps({'frontend_pid':111,'frontend_start_ticks':10,'frontend_epoch':'previous'}))
            for name in ('launch.sh','reset.py'):(root/name).write_text('')
            args=argparse.Namespace(execute=True,study_dir=root,state=state_path,reset_only=False,
                                    launch_script=root/'launch.sh', reset_script=root/'reset.py',condition='fixed',base_url='http://127.0.0.1:8000/v1')
            child=Mock(pid=222); child.poll.return_value=None
            with patch.object(hook.sys,'platform','linux'), patch.object(os,'pidfd_open',create=True), patch.object(hook.signal,'pidfd_send_signal',create=True), patch.object(hook,'require_owned_frontend',return_value=(111,10)), patch.object(hook,'terminate_owned') as stop, patch.object(subprocess,'Popen',return_value=child), patch.object(hook,'start_ticks',return_value=20), patch.object(hook,'request_json',side_effect=[{'data':[{'id':'Qwen/Qwen3-8B'}]},{'choices':[{}]}]), patch.object(hook,'reset_worker',return_value={'backend_reset_reported':True}), patch.object(hook,'clear_proof',return_value=trace()):
                result=hook.run(args)
            stop.assert_called_once_with(111,10)
            self.assertTrue(result['router_index_empty'])
            self.assertEqual(result['previous_frontend_epoch'],'previous')
            self.assertNotEqual(result['frontend_epoch'],'previous')
            proof=Path(result['proof_file'])
            self.assertTrue(proof.is_absolute())
            self.assertEqual(result['proof_sha256'],hashlib.sha256(proof.read_bytes()).hexdigest())
            self.assertEqual(json.loads(state_path.read_text())['status'],'ready')

    def test_metrics_hook_keeps_canary_raw_clear_identity_and_snapshot_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_path = root / 'state.json'
            state_path.write_text(json.dumps({'frontend_pid': 111, 'frontend_start_ticks': 10, 'frontend_epoch': 'previous'}))
            for name in ('launch.sh', 'reset.py'):
                (root / name).write_text('')
            args = argparse.Namespace(execute=True, study_dir=root, state=state_path, reset_only=False,
                launch_script=root/'launch.sh', reset_script=root/'reset.py', condition='fixed',
                base_url='http://127.0.0.1:8000/v1', router_clear_proof='metrics')
            child = Mock(pid=222)
            child.poll.return_value = None
            with patch.object(hook.sys, 'platform', 'linux'), patch.object(os, 'pidfd_open', create=True), \
                    patch.object(hook.signal, 'pidfd_send_signal', create=True), \
                    patch.object(hook, 'require_owned_frontend', return_value=(111, 10)), \
                    patch.object(hook, 'terminate_owned'), patch.object(subprocess, 'Popen', return_value=child), \
                    patch.object(hook, 'start_ticks', return_value=20), \
                    patch.object(hook, 'request_json', side_effect=[{'data': [{'id': 'Qwen/Qwen3-8B'}]},
                        {'usage': {'prompt_tokens_details': {'cached_tokens': 0}}}]), \
                    patch.object(hook, 'reset_worker', return_value={'backend_reset_reported': True}), \
                    patch.object(hook, 'require_concurrent_main', return_value={'router_event_threads': 4}) as config, \
                    patch.object(hook, 'require_raw_clear', return_value={'sequence': 9}) as clear, \
                    patch.object(hook, 'read_frontend_metrics', side_effect=[metrics(), metrics(cleared=1)]), \
                    patch.object(hook, 'clear_proof') as trace_lookup:
                result = hook.run(args)
            config.assert_called()
            self.assertEqual(config.call_count, 2)
            clear.assert_called_once()
            trace_lookup.assert_not_called()
            evidence = json.loads(Path(result['proof_file']).read_text())
            self.assertEqual(evidence['raw_clear'], {'sequence': 9})
            self.assertEqual(evidence['metrics_before_sha256'], hashlib.sha256(metrics()).hexdigest())
            self.assertEqual(evidence['metrics_after_sha256'], hashlib.sha256(metrics(cleared=1)).hexdigest())
            self.assertTrue(result['router_index_empty'])
            self.assertNotIn('trace_record', evidence)

    def test_reset_failure_stops_new_owned_child_and_marks_failed(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); state=root/'state.json'
            state.write_text(json.dumps({'frontend_pid':111,'frontend_start_ticks':10,'frontend_epoch':'old'}))
            for name in ('launch.sh','reset.py'):(root/name).write_text('')
            args=argparse.Namespace(execute=True,study_dir=root,state=state,reset_only=False,launch_script=root/'launch.sh',reset_script=root/'reset.py',condition='stock',base_url='http://127.0.0.1:8000/v1')
            child=Mock(pid=222); child.poll.return_value=None
            with patch.object(hook.sys,'platform','linux'), patch.object(os,'pidfd_open',create=True), patch.object(hook.signal,'pidfd_send_signal',create=True), patch.object(hook,'require_owned_frontend',return_value=(111,10)), patch.object(hook,'terminate_owned') as stop, patch.object(subprocess,'Popen',return_value=child), patch.object(hook,'start_ticks',return_value=20), patch.object(hook,'request_json',return_value={'data':[{'id':'Qwen/Qwen3-8B'}]}), patch.object(hook,'reset_worker',side_effect=RuntimeError('reset failed')):
                with self.assertRaisesRegex(RuntimeError,'reset failed'):hook.run(args)
            self.assertEqual(stop.call_count,2)
            self.assertEqual(stop.call_args.args,(222,20))
            self.assertEqual(json.loads(state.read_text())['status'],'failed')


if __name__=='__main__':unittest.main()
