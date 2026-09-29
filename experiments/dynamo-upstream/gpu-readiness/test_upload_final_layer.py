import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location("upload_final_layer", Path(__file__).with_name("upload_final_layer.py"))
upload = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upload)


class Registry:
    def __init__(self, data):
        self.expected = data
        self.data = b""
        self.done = False
        self.calls = []
        self.partial = None
        self.url = upload.ROOT + "/blobs/uploads/test-upload"
        self.digest = "sha256:" + hashlib.sha256(data).hexdigest()

    def __call__(self, method, url, data=None, headers=None):
        self.calls.append(method)
        if method == "HEAD":
            return (200, {"Content-Length": str(len(self.expected)), "Docker-Content-Digest": self.digest}) if self.done else (404, {})
        if method == "POST":
            return 202, {"Location": self.url, "Range": "0-0"}
        if method == "GET":
            return 204, {"Location": self.url, "Range": f"0-{max(0, len(self.data)-1)}"}
        if method == "PATCH":
            start, end = map(int, headers["Content-Range"].split("-"))
            assert start == len(self.data)
            assert end == start + len(data) - 1
            assert int(headers["Content-Length"]) == len(data)
            if self.partial is not None:
                self.data += data[:self.partial]
                self.partial = None
                raise TimeoutError("uncertain write")
            self.data += data
            return 202, {"Location": self.url, "Range": f"0-{len(self.data)-1}"}
        if method == "PUT":
            assert data == b""
            assert self.data == self.expected
            self.done = True
            return 201, {}
        raise AssertionError(method)


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = b"abcdefghijklmnopqrstuvwxyz012345"
        self.file = self.root / "blob"
        self.file.write_bytes(self.data)
        self.registry = Registry(self.data)

    def uploader(self):
        source = self.file.open("rb")
        self.addCleanup(source.close)
        return upload.Uploader(source, self.root / "state.json", self.registry,
                               self.registry.digest, len(self.data), 8)

    def test_chunks_reconcile_and_final_digest(self):
        worker = self.uploader()
        worker.run()
        self.assertTrue(self.registry.done)
        self.assertEqual(worker.state["stage"], "complete")
        for position, method in enumerate(self.registry.calls):
            if method == "PATCH":
                self.assertEqual(self.registry.calls[position + 1], "GET")

    def test_uncertain_partial_write_resumes_from_get(self):
        self.registry.partial = 3
        self.uploader().run()
        self.assertEqual(self.registry.data, self.data)
        self.assertEqual(self.registry.calls.count("POST"), 1)

    def test_ambiguous_empty_range_stops(self):
        self.registry.partial = 0
        with self.assertRaisesRegex(upload.UploadError, "ambiguous"):
            self.uploader().run()
        self.assertEqual(self.registry.calls.count("PATCH"), 1)

    def test_adopt_probe_then_resume_checkpoint(self):
        self.registry.data = self.data[:3]
        headers = {"Location": self.registry.url, "Range": "0-2"}
        probe = self.root / "probe.json"
        probe.write_text(json.dumps({"digest": self.registry.digest, "size": len(self.data),
            "location": self.registry.url, "patch": {"status": 202, "headers": headers},
            "get": {"status": 204, "headers": headers}}))
        worker = self.uploader()
        worker.adopt_probe(probe)
        worker.run(max_chunks=1)
        self.assertEqual(worker.state["offset"], 11)
        self.uploader().run()
        self.assertTrue(self.registry.done)
        self.assertNotIn("POST", self.registry.calls)

    def test_hash_mismatch_prevents_every_request(self):
        self.file.write_bytes(b"X" * len(self.data))
        with self.assertRaisesRegex(upload.UploadError, "SHA-256"):
            self.uploader()
        self.assertEqual(self.registry.calls, [])

    def test_cross_origin_location_rejected(self):
        with self.assertRaises(upload.UploadError):
            upload.location("https://other.example/blobs/uploads/test")


if __name__ == "__main__":
    unittest.main()
