import json
from pathlib import Path
import tempfile
import threading
import unittest

from capture_worker import Capture, write_close_status
from validate_capture import validate_capture


class ValidateTests(unittest.TestCase):
    def test_complete_capture_validates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "engine.jsonl"
            capture = Capture(path)
            write_close_status(path, capture.close())
            self.assertTrue(validate_capture(path)["valid_capture"])

    def test_missing_close_status_and_missing_sequence_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "engine.jsonl"
            capture = Capture(path)
            status = capture.close()
            with self.assertRaises(FileNotFoundError):
                validate_capture(path)
            write_close_status(path, status)
            rows = path.read_text().splitlines()
            event = json.loads(rows[0])
            event["sequence"] = 8
            rows[0] = json.dumps(event)
            path.write_text("\n".join(rows) + "\n")
            with self.assertRaisesRegex(ValueError, "sequence"):
                validate_capture(path)

    def test_slow_footer_cannot_override_authoritative_failed_close(self):
        class SlowFooter:
            def __init__(self, path):
                self.stream = path.open("w")
                self.release = threading.Event()
                self.entered = threading.Event()

            def write(self, text):
                if json.loads(text)["kind"] == "writer_summary":
                    self.entered.set()
                    self.release.wait()
                self.stream.write(text)

            def flush(self):
                self.stream.flush()

            def close(self):
                self.stream.close()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "engine.jsonl"
            sink = SlowFooter(path)
            capture = Capture(path, stream=sink, close_timeout=0.05)
            try:
                self.assertTrue(capture.drain())
                status = capture.close()
                self.assertTrue(sink.entered.is_set())
                self.assertFalse(status["valid_capture"])
                write_close_status(path, status)
            finally:
                sink.release.set()
                capture.thread.join(timeout=1)
            # The footer was assembled before close timed out.
            self.assertTrue(json.loads(path.read_text().splitlines()[-1])["valid_capture"])
            with self.assertRaisesRegex(ValueError, "Authoritative"):
                validate_capture(path)

    def test_stream_close_failure_is_retained(self):
        class BadClose:
            def write(self, text):
                pass

            def flush(self):
                pass

            def close(self):
                raise OSError("close failed")

        capture = Capture("unused", stream=BadClose())
        status = capture.close()
        self.assertFalse(status["valid_capture"])
        self.assertEqual(status["failures"][-1]["stage"], "stream_close")
