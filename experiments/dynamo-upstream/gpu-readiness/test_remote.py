import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import remote


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "file"
        self.data = b"complete study evidence\n"
        self.path.write_bytes(self.data)
        self.digest = {"size": len(self.data), "sha256": hashlib.sha256(self.data).hexdigest()}
        self.status = 1
        self.remote_status = 0
        self.remote_values = []
        self.options = ["-i", "/private/study-key", "-o", "IdentitiesOnly=yes"]
        self.calls = []

    def fake_run(self, command, **kwargs):
        self.calls.append(command)
        self.assertEqual(command[1:1 + len(self.options)], self.options)
        if command[0] == "scp":
            return SimpleNamespace(returncode=self.status)
        value = self.remote_values.pop(0) if self.remote_values else self.digest
        return SimpleNamespace(returncode=self.remote_status, stdout=json.dumps(value).encode())

    def transfer(self, operation):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = remote.transfer(operation, self.path, "/tmp/reedcode-evidence.tar.gz",
                                     "root@127.0.0.1", 2222, self.options, self.fake_run)
        return result, json.loads(output.getvalue())

    def test_upload_exit_one_with_full_match_is_verified(self):
        result, proof = self.transfer("upload")
        self.assertEqual(result, 0)
        self.assertTrue(proof["transfer_verified"])
        self.assertEqual(proof["scp_exit_code"], 1)

    def test_download_exit_one_with_full_match_is_verified(self):
        result, _ = self.transfer("download")
        self.assertEqual(result, 0)
        self.assertEqual([c[0] for c in self.calls], ["ssh", "scp", "ssh"])

    def test_size_mismatch_fails_even_after_scp_success(self):
        self.status = 0
        self.remote_values = [{**self.digest, "size": 1}]
        with self.assertRaisesRegex(ValueError, "hashes or sizes differ"):
            self.transfer("upload")

    def test_hash_mismatch_does_not_ignore_exit_one(self):
        self.remote_values = [{**self.digest, "sha256": "0" * 64}]
        with self.assertRaisesRegex(ValueError, "hashes or sizes differ"):
            self.transfer("upload")

    def test_missing_remote_file_fails(self):
        self.remote_status = 1
        with self.assertRaisesRegex(ValueError, "could not be verified"):
            self.transfer("upload")

    def test_missing_local_file_fails(self):
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.transfer("upload")
        self.assertEqual(self.calls, [])

    def test_changing_remote_archive_fails(self):
        self.remote_values = [self.digest, {**self.digest, "sha256": "0" * 64}]
        with self.assertRaisesRegex(ValueError, "hashes or sizes differ"):
            self.transfer("download")


if __name__ == "__main__":
    unittest.main()
