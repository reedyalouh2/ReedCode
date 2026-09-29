import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from capture_proxy import CaptureServer


FIRST = 'event: token\ndata: {"text":"π"}\n\n'.encode()
LAST = b"event: done\ndata: {}\n\n"


class Echo(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_POST(self):
        self.server.bodies.append(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.trace_ids.append(self.headers.get_all("X-Request-ID", []))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        if self.server.fixed_length:
            self.send_header("Content-Length", str(len(FIRST + LAST) + self.server.extra_length))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(FIRST)
        self.wfile.flush()
        time.sleep(0.15)
        try:
            self.wfile.write(LAST)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reedcode-proxy-test-")
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
        self.upstream.bodies = []
        self.upstream.trace_ids = []
        self.upstream.fixed_length = False
        self.upstream.extra_length = 0
        self.proxy = CaptureServer(("127.0.0.1", 0), f"http://127.0.0.1:{self.upstream.server_port}",
                                   Path(self.temp.name) / "capture", 1, 3)
        for server in (self.upstream, self.proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def tearDown(self):
        for server in (self.proxy, self.upstream):
            server.shutdown()
            server.server_close()
        self.temp.cleanup()

    def request(self, body=b' { "model" : "Qwen/Qwen3-8B" }\n', headers=None, chunked=False):
        connection = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=2)
        connection.request("POST", "/v1/responses", body=body, headers=headers or {}, encode_chunked=chunked)
        return connection, connection.getresponse()

    def metadata(self, seq=1):
        path = Path(self.temp.name) / "capture" / f"{seq:04d}" / "metadata.json"
        for _ in range(50):
            if path.exists():
                return json.loads(path.read_text())
            time.sleep(0.01)
        self.fail("capture metadata was not completed")

    def test_original_bytes_and_incremental_sse(self):
        original = ' { "x" : "π", "tools": [] }\n'.encode()
        connection, response = self.request(original)
        start = time.monotonic()
        self.assertEqual(response.read(len(FIRST)), FIRST)
        self.assertLess(time.monotonic() - start, 0.1)
        self.assertEqual(response.read(), LAST)
        connection.close()
        meta = self.metadata()
        self.assertEqual(self.upstream.bodies, [original])
        self.assertEqual(meta["request_sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(meta["response_sha256"], hashlib.sha256(FIRST + LAST).hexdigest())
        self.assertTrue(meta["complete"])

    def test_chunked_request_preserves_entity_bytes(self):
        connection, response = self.request(iter([b'{"a":', b'1}']), chunked=True)
        response.read()
        connection.close()
        self.assertEqual(self.upstream.bodies, [b'{"a":1}'])
        self.assertTrue(self.metadata()["complete"])

    def test_fixed_length_response_completes_without_touching_closed_socket(self):
        self.upstream.fixed_length = True
        connection, response = self.request()
        self.assertEqual(response.read(), FIRST + LAST)
        connection.close()
        self.assertTrue(self.metadata()["complete"])

    def test_short_fixed_length_response_is_incomplete(self):
        self.upstream.fixed_length = True
        self.upstream.extra_length = 10
        connection, response = self.request()
        with self.assertRaises(http.client.IncompleteRead):
            response.read()
        connection.close()
        meta = self.metadata()
        self.assertFalse(meta["complete"])
        self.assertIn("IncompleteRead", meta["error"])

    def test_count_cap_blocks_before_upstream(self):
        connection, response = self.request()
        response.read()
        connection.close()
        connection, response = self.request()
        self.assertEqual(response.status, 429)
        response.read()
        connection.close()
        self.assertEqual(len(self.upstream.bodies), 1)
        self.assertEqual(self.metadata(2)["error"], "session call or time limit")

    def test_fifteenth_call_is_admitted_and_sixteenth_is_blocked(self):
        self.proxy.max_calls = 15
        self.proxy.seconds = 10
        statuses = []
        for _ in range(16):
            connection, response = self.request()
            statuses.append(response.status)
            response.read()
            connection.close()
        self.assertEqual(statuses, [200] * 15 + [429])
        self.assertEqual(len(self.upstream.bodies), 15)

    def test_duplicate_authorization_cannot_bypass_guard(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=2)
        connection.putrequest("POST", "/v1/responses")
        connection.putheader("Authorization", "Bearer local-dynamo")
        connection.putheader("Authorization", "Bearer unexpected-test-value")
        connection.putheader("Content-Length", "0")
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        connection.close()
        self.assertTrue(self.metadata()["unexpected_credentials"])
        self.assertEqual(self.upstream.bodies, [])

    def test_credential_rejection_does_not_record_token_or_body(self):
        connection, response = self.request(b"private body", {"Authorization": "Bearer unexpected-test-value"})
        self.assertEqual(response.status, 403)
        response.read()
        connection.close()
        meta = self.metadata()
        self.assertTrue(meta["unexpected_credentials"])
        self.assertFalse((Path(self.temp.name) / "capture/0001/request.body").exists())
        self.assertNotIn("unexpected-test-value", json.dumps(meta))
        self.assertEqual(self.upstream.bodies, [])

    def test_deadline_interrupts_inflight_stream(self):
        self.proxy.seconds = 0.04
        connection, response = self.request()
        self.assertEqual(response.read(), FIRST)
        connection.close()
        meta = self.metadata()
        self.assertFalse(meta["complete"])
        self.assertIn("Timeout", meta["error"])

    def test_controller_shutdown_keeps_partial_capture(self):
        connection, response = self.request()
        self.assertEqual(response.read(len(FIRST)), FIRST)
        self.proxy.cancel_active()
        response.read()
        connection.close()
        meta = self.metadata()
        self.assertFalse(meta["complete"])
        self.assertIn("controller stopped", meta["error"])

    def test_observer_trace_id_is_separate_from_original_headers_and_body(self):
        self.proxy.trace_id_prefix = "parity-test"
        original = b'{"model":"unchanged","tools": []}\n'
        connection, response = self.request(original)
        response.read()
        connection.close()
        meta = self.metadata()
        trace_id = meta["observer_x_request_id"]
        self.assertTrue(trace_id.startswith("parity-test-"))
        self.assertEqual(meta["original_x_request_ids"], [])
        self.assertEqual(meta["injected_request_headers"], [["X-Request-ID", trace_id]])
        self.assertEqual(self.upstream.trace_ids, [[trace_id]])
        self.assertEqual(self.upstream.bodies, [original])
        self.assertFalse(any(key.lower() == "x-request-id" for key, _ in meta["request_headers"]))

    def test_existing_client_trace_id_is_preserved_without_injection(self):
        self.proxy.trace_id_prefix = "parity-test"
        connection, response = self.request(headers={"X-Request-ID": "client-owned-id"})
        response.read()
        connection.close()
        meta = self.metadata()
        self.assertEqual(meta["original_x_request_ids"], ["client-owned-id"])
        self.assertIsNone(meta["observer_x_request_id"])
        self.assertEqual(meta["injected_request_headers"], [])
        self.assertEqual(self.upstream.trace_ids, [["client-owned-id"]])


if __name__ == "__main__":
    unittest.main()
