from pathlib import Path
import tempfile
import unittest

from launch_study import REPOSITORY, command_for, prepare_task, tunnel_port


class LaunchTests(unittest.TestCase):
    def test_only_explicit_local_tunnel_origins(self):
        self.assertEqual(tunnel_port("http://127.0.0.1:18002"), 18002)
        for value in ("https://127.0.0.1:18002", "http://example.com:18002",
                      "http://token@127.0.0.1:18002", "http://127.0.0.1:18002/v1"):
            with self.assertRaises(ValueError):
                tunnel_port(value)

    def test_public_snapshot_hashes_and_fresh_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "study"
            result = prepare_task(workspace, REPOSITORY)
            self.assertEqual(result["revision"], "d72cf6dfba2660e1571ed1aa3a0d798e7ee67b31")
            self.assertTrue((workspace / "task/output_policy.py").is_file())
            self.assertFalse((workspace / "task/.git").exists())
            with self.assertRaises(FileExistsError):
                prepare_task(workspace, REPOSITORY)

    def test_codex_command_keeps_verified_isolation(self):
        command = command_for("codex", "smoke", Path("/tmp/study"), Path("/tmp/codex"),
                              "http://127.0.0.1:18012", 180, "docker", "pinned-image", "unique", [])
        self.assertIn("--smoke", command)
        self.assertIn("18012", command)
        self.assertEqual(command[command.index("--pull") + 1], "never")
        self.assertEqual(command[command.index("--platform") + 1], "linux/arm64")
        mounts = [command[i + 1] for i, value in enumerate(command[:-1]) if value == "-v"]
        self.assertEqual(len(mounts), 4)
        self.assertEqual(mounts[-1], "/tmp/study:/study")
        self.assertTrue(all(value.endswith(":ro") for value in mounts[:-1]))

    def test_claude_command_preserves_deny_paths(self):
        command = command_for("claude", "full", Path("/tmp/study"), Path("/tmp/claude"),
                              "http://127.0.0.1:18002", 1500, "docker", "image", "unique",
                              [Path("/tmp/private-study")])
        self.assertNotIn("--smoke", command)
        self.assertEqual(command[command.index("--deny-read") + 1], "/tmp/private-study")
        self.assertEqual(command[command.index("--max-calls") + 1], "15")


if __name__ == "__main__":
    unittest.main()
