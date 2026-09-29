from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from check_clients import sandbox_profile


@unittest.skipUnless(sys.platform == "darwin", "macOS sandbox test")
class ProfileTests(unittest.TestCase):
    def test_explicit_dummy_credentials_are_denied_while_task_is_readable(self):
        with tempfile.TemporaryDirectory(prefix="reedcode-profile-task-") as task_dir:
            with tempfile.TemporaryDirectory(prefix="reedcode-dummy-auth-") as denied_dir:
                task = Path(task_dir).resolve()
                dummy = Path(denied_dir).resolve()
                (task / "public.txt").write_text("PUBLIC_FIXTURE")
                (dummy / "dummy.txt").write_text("DUMMY_VALUE")
                profile = sandbox_profile(task, [dummy])
                code = """import pathlib, sys
assert pathlib.Path(sys.argv[1]).read_text() == 'PUBLIC_FIXTURE'
try:
    pathlib.Path(sys.argv[2]).read_text()
except PermissionError:
    print('task_read_allowed=true dummy_credential_read_denied=true')
else:
    raise SystemExit('dummy credential was readable')
"""
                result = subprocess.run(["/usr/bin/sandbox-exec", "-f", str(profile), sys.executable,
                                         "-c", code, str(task / "public.txt"), str(dummy / "dummy.txt")],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("dummy_credential_read_denied=true", result.stdout)
                self.assertIn(str(Path("/tmp/reedcode-gpu-credentials").resolve()), profile.read_text())
                self.assertIn(str(Path("/tmp/reedcode-runpod-auth").resolve()), profile.read_text())
                self.assertIn(str(Path("/tmp/reedcode-prefix-auth").resolve()), profile.read_text())


if __name__ == "__main__":
    unittest.main()
