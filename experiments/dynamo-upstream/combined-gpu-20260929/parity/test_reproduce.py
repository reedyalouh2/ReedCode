import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('parity_reproduction', HERE / 'reproduce.py')
REPRO = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPRO)


class ControllerEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.clients = self.root / 'clients'
        REPRO.unpack_clients(self.clients)
        self.cases = {row['name']: row for row in REPRO.read(HERE / 'cases.json')}

    def combine(self, case):
        return REPRO.combine_controllers(self.cases[case], self.clients, self.root / 'derived')

    def test_budget_attempts_stay_separate_from_admitted_requests(self):
        controller, ledger = self.combine('claude-yarn-64k')
        saved = REPRO.read(self.root / 'derived/capture-ledger.json')
        self.assertTrue(controller['completed'])
        self.assertEqual(len(ledger), 15)
        self.assertEqual([row['admitted_inference_count'] for row in saved['controller_only_attempts']], [7, 11, 15])
        self.assertEqual([row['user_turn'] for row in ledger], [1] * 7 + [2] * 4 + [3] * 4)

    def test_missing_body_after_admission_fails(self):
        path = self.clients / 'claude-yarn-64k/capture/0008/metadata.json'
        value = REPRO.read(path)
        value['admitted_inference_count'] = 8
        REPRO.save(path, value)
        with self.assertRaisesRegex(ValueError, 'Unexplained capture'):
            self.combine('claude-yarn-64k')

    def test_changed_http_body_fails(self):
        path = self.clients / 'codex-32k/capture/0001/request.body'
        path.write_bytes(path.read_bytes() + b' ')
        with self.assertRaisesRegex(ValueError, 'Client body hash mismatch'):
            self.combine('codex-32k')

    def test_original_server_errors_and_continuation_remain_visible(self):
        controller, ledger = self.combine('claude-32k')
        self.assertFalse(controller['completed'])
        self.assertEqual([row['http_status'] for row in ledger], [200] * 4 + [400] * 3)
        self.assertEqual([row['user_turn'] for row in ledger], [1] * 5 + [2, 3])
        for row in ledger[4:]:
            self.assertIn('32768 combined input and output tokens', row['server_error']['error']['message'])


if __name__ == '__main__':
    unittest.main()
