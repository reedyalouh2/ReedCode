"""Check relocated evidence and explicit accounting for unfinished trials."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import analyze_snapshot as analysis


EPOCH = 'prefill/epochs/example'


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'raw'
        self.root.mkdir()
        self.wire = Path(self.temp.name) / 'wire.json'

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())
        return path

    def manifest(self):
        files = {str(path.relative_to(self.root)): analysis.sha_file(path)
                 for path in self.root.rglob('*') if path.is_file() and path.name != 'snapshot-manifest.json'}
        self.write('snapshot-manifest.json', {'files': files, 'final_after_process_stop': True})
        return files

    def trial(self, status='replay_completed', condition='off'):
        directory = EPOCH + '/trials/short-' + condition + '-r1'
        proof = self.write(EPOCH + '/frontend/proof-' + condition + '.json', {'clear': True})
        frontend = {'router_index_empty': True, 'frontend_pid': 123, 'frontend_epoch': condition,
                    'started_unix': 1, 'proof_file': '/tmp/reedcode-study/' + str(proof.relative_to(self.root)),
                    'proof_sha256': analysis.sha_file(proof)}
        self.write(directory + '/trial.json', {'status': status, 'session': 'short', 'repeat': 1,
                   'condition': condition, 'frontend': frontend, 'error': 'interrupted' if status != 'replay_completed' else None})
        self.write(directory + '/kv-config.json', {})
        self.write(directory + '/kv-frames.jsonl', b'')
        self.write(directory + '/replay/run.json', {'files': {'requests.jsonl': hashlib.sha256(b'').hexdigest()}})
        self.write(directory + '/replay/requests.jsonl', b'')
        return directory, frontend

    def capture(self):
        pcap = self.write(EPOCH + '/wire.pcap', b'test capture')
        self.write(EPOCH + '/capture-final.json', {'zero_drops_verified': True})
        self.wire.write_text(json.dumps({'pcap_sha256': analysis.sha_file(pcap)}))

    def test_empty_snapshot_keeps_all_planned_cells_and_pairs(self):
        self.manifest()
        result = analysis.analyze(self.root)
        self.assertEqual(18, len(result['cells']))
        self.assertEqual({'not_started'}, {cell['status'] for cell in result['cells']})
        self.assertEqual(18, len(result['paired_differences']))
        self.assertFalse(any(row['complete_pair'] for row in result['paired_differences']))
        self.assertEqual(0, result['completed_reports'])

    def test_partial_trial_and_directory_remain_explicit(self):
        self.trial(status='failed')
        self.write(EPOCH + '/trials/short-stock-r1/fresh-frontend.stderr', b'starting')
        self.manifest()
        result = analysis.analyze(self.root)
        self.assertEqual(['incomplete', 'incomplete', 'not_started'], [cell['status'] for cell in result['cells'][:3]])
        self.assertEqual('interrupted', result['cells'][0]['recorded_error'])
        self.assertEqual(18, len(result['cells']))

    def test_completed_trial_without_capture_is_analysis_unavailable(self):
        self.trial()
        self.manifest()
        result = analysis.analyze(self.root)
        self.assertEqual('analysis_unavailable', result['cells'][0]['status'])

    def test_verified_proof_is_relocated_in_memory_and_raw_stays_identical(self):
        directory, frontend = self.trial()
        self.capture()
        files = self.manifest()
        fake = {'session': 'short', 'repeat': 1, 'condition': 'off'}
        with patch.object(analysis, 'report', return_value=fake) as report:
            result = analysis.analyze(self.root, '/tmp/reedcode-study/' + EPOCH, self.wire)
        report.assert_called_once_with(self.root.resolve() / directory, self.wire)
        self.assertEqual('completed', result['cells'][0]['status'])
        self.assertEqual(1, result['completed_reports'])
        self.assertEqual(frontend, json.loads((self.root / directory / 'trial.json').read_text())['frontend'])
        self.assertEqual(files, {name: analysis.sha_file(self.root / name) for name in files})

    def test_modified_member_rejected_before_analysis(self):
        self.trial()
        self.manifest()
        self.write(EPOCH + '/frontend/proof-off.json', {'clear': False})
        with self.assertRaisesRegex(ValueError, 'missing or changed'):
            analysis.analyze(self.root)

    def test_only_completed_pairs_have_differences(self):
        self.trial(condition='off')
        self.trial(condition='stock')
        self.capture()
        self.manifest()
        def report(directory, _wire):
            trial = json.loads((directory / 'trial.json').read_text())
            return {key: trial[key] for key in ('session', 'repeat', 'condition')}
        with patch.object(analysis, 'report', side_effect=report), patch.object(
                analysis, 'metrics', side_effect=lambda trial: {'count': 10 if trial['condition'] == 'off' else 12}):
            result = analysis.analyze(self.root, wire=self.wire)
        pairs = result['paired_differences']
        self.assertEqual(18, len(pairs))
        self.assertEqual(1, sum(row['complete_pair'] for row in pairs))
        self.assertEqual({'count': 2}, pairs[0]['difference'])
        self.assertNotIn('difference', pairs[1])

    def test_external_and_traversal_paths_rejected(self):
        for path in ('/tmp/other/x', 'relative', '/tmp/reedcode-study/../other/x'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                analysis.relocate(self.root, path)

    def test_multiple_epochs_require_selection(self):
        self.trial(status='failed')
        self.write('prefill/epochs/another/state.json', {})
        self.manifest()
        with self.assertRaisesRegex(ValueError, 'Multiple prefill epochs'):
            analysis.analyze(self.root)
        result = analysis.analyze(self.root, EPOCH)
        self.assertEqual(EPOCH, result['provenance']['selected_epoch'])

    def test_mismatched_pcap_and_unknown_capture_drops_rejected(self):
        self.trial()
        self.capture()
        self.manifest()
        self.wire.write_text(json.dumps({'pcap_sha256': 'different'}))
        with self.assertRaisesRegex(ValueError, 'hash differs'):
            analysis.analyze(self.root, wire=self.wire)
        self.capture()
        self.write(EPOCH + '/capture-final.json', {'zero_drops_verified': None})
        self.manifest()
        with self.assertRaisesRegex(ValueError, 'zero-drop'):
            analysis.analyze(self.root, wire=self.wire)

    def test_unlisted_replay_evidence_is_rejected(self):
        directory, _ = self.trial()
        self.capture()
        self.manifest()
        manifest_path = self.root / 'snapshot-manifest.json'
        manifest = json.loads(manifest_path.read_text())
        del manifest['files'][directory + '/kv-frames.jsonl']
        manifest_path.write_text(json.dumps(manifest))
        with patch.object(analysis, 'report') as report:
            result = analysis.analyze(self.root, wire=self.wire)
        report.assert_not_called()
        self.assertEqual('analysis_failed', result['cells'][0]['status'])
        self.assertIn('absent from the verified', result['cells'][0]['error'])


if __name__ == '__main__':
    unittest.main()
