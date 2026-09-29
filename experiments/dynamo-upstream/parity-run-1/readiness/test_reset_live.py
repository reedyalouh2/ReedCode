import importlib.util
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('parity_reset_live', ROOT / 'reset_live.py')
reset = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reset)


class ParityResetTests(unittest.TestCase):
    def live_fixture(self, mode='first-request', cached=0):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        study = Path(temporary.name)
        folder = study / 'parity'
        kv = folder / 'kv'
        kv.mkdir(parents=True)
        (study / 'bootstrap').mkdir()
        (study / 'bootstrap/identity.json').write_text(json.dumps({'python': '/test/python'}))
        (kv / 'config.json').write_text(json.dumps({'hostname': reset.socket.gethostname(),
            'operator_supplied_worker_epoch': 'epoch', 'topic': 'kv-events'}))
        (kv / 'frames.jsonl').write_text('{}\n')
        (folder / 'processes.json').write_text(json.dumps({'backend': {'pid': 10, 'start_ticks': 42, 'epoch': 'epoch'}}))
        log = folder / 'frontend.log'
        log.write_text('no clear trace\n')
        (folder / 'frontend-state.json').write_text(json.dumps({'frontend_epoch': 'front', 'stdout_path': str(log)}))
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(reset.sys, 'platform', 'linux'))
        stack.enter_context(patch.object(reset.hook, 'start_ticks', return_value=42))
        stack.enter_context(patch.object(reset.hook, 'require_owned_frontend'))
        clear = stack.enter_context(patch.object(reset.hook, 'clear_proof', return_value=None))
        stack.enter_context(patch.object(reset.identity, 'process_tree', return_value=[{'pid': 10}]))
        labels = 'model_name="Qwen/Qwen3-8B",engine="0"'
        response = MagicMock()
        response.__enter__.return_value.read.return_value = (f'vllm:num_requests_running{{{labels}}} 0\n'
            f'vllm:num_requests_waiting{{{labels}}} 0\n').encode()
        stack.enter_context(patch.object(reset, 'urlopen', return_value=response))
        stack.enter_context(patch.object(reset.subprocess, 'run', return_value=SimpleNamespace(
            returncode=0, stdout=b'{"backend_reset_reported":true}', stderr=b'')))
        stack.enter_context(patch.object(reset, 'send', return_value=({'cached_tokens': cached}, None)))
        kv_clear = stack.enter_context(patch.object(reset, 'wait_clear', return_value={'sequence': 1}))
        return SimpleNamespace(study_dir=study, client='claude', execute=True, backend_python=None,
                               clean_start_proof=mode), clear, kv_clear

    def test_first_request_mode_keeps_real_proof_pending_and_still_requires_kv_clear(self):
        args, clear, kv_clear = self.live_fixture()
        result = reset.run(args)
        self.assertEqual(result['status'], 'reset_awaiting_first_request')
        self.assertIsNone(result['router_index_empty'])
        self.assertTrue(result['backend_cache_empty'])
        self.assertEqual(result['first_real_request_proof']['status'], 'pending')
        self.assertEqual(result['first_real_request_proof']['required_cached_tokens'], 0)
        clear.assert_not_called()
        kv_clear.assert_called_once()
        self.assertFalse((Path(result['output']) / 'frontend-clear-row.json').exists())

    def test_trace_mode_still_fails_without_current_frontend_clear(self):
        args, clear, _ = self.live_fixture('frontend-trace')
        with patch.object(reset.time, 'monotonic', side_effect=[0, 6]):
            with self.assertRaisesRegex(TimeoutError, 'frontend clear'):
                reset.run(args)
        clear.assert_called_once()

    def test_first_request_mode_does_not_relax_canary_cache_check(self):
        args, _, _ = self.live_fixture(cached=16)
        with self.assertRaisesRegex(ValueError, 'canary unexpectedly reused'):
            reset.run(args)

    def test_parity_discovery_replaces_inherited_prefill_and_occupied_ports(self):
        inherited = {'DYN_FILE_KV': '/study/prefill/discovery', 'DYN_TCP_RPC_PORT': '20002',
                     'DYN_TCP_RESPONSE_STREAM_PORT': '20003', 'KEEP': 'value'}
        env = reset.reset_environment(Path('/study'), inherited)
        self.assertEqual(env['DYN_FILE_KV'], '/study/parity/discovery')
        self.assertEqual(env['DYN_REQUEST_PLANE'], 'tcp')
        self.assertEqual(env['DYN_SYSTEM_PORT'], '-1')
        self.assertEqual(env['KEEP'], 'value')
        self.assertNotIn('DYN_TCP_RPC_PORT', env)
        self.assertNotIn('DYN_TCP_RESPONSE_STREAM_PORT', env)
        self.assertEqual(inherited['DYN_FILE_KV'], '/study/prefill/discovery')

    def test_canary_cannot_fill_a_full_block_and_has_no_hint(self):
        payload = reset.canary_payload()
        self.assertEqual(payload['prompt'], [42])
        self.assertEqual(payload['max_tokens'], 1)
        self.assertNotIn('nvext', payload)
        self.assertLess(len(payload['prompt']) + payload['max_tokens'], 16)

    def test_reset_requires_observable_idle_engine_zero(self):
        labels = 'model_name="Qwen/Qwen3-8B",engine="0"'
        idle = (f'vllm:num_requests_running{{{labels}}} 0\n'
                f'vllm:num_requests_waiting{{{labels}}} 0\n').encode()
        reset.require_idle(idle)
        for raw in (b'', idle.replace(b'} 0', b'} 1'), idle.replace(b'engine="0"', b'engine="1"')):
            with self.assertRaisesRegex(ValueError, 'idle, single-engine'):
                reset.require_idle(raw)


if __name__ == '__main__':
    unittest.main()
