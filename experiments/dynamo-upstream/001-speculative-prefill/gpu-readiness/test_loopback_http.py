from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import threading
import unittest
from unittest.mock import patch

import replay


class Endpoint(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.server.paths.append(self.path)
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", self.server.redirect_to)
            self.end_headers()
            return
        body = self.server.body
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class LoopbackHTTPTests(unittest.TestCase):
    def setUp(self):
        self.target = self.start_server(b"local metrics")
        self.other = self.start_server(b"proxy response")
        self.target.redirect_to = self.origin(self.other) + "/private"

    def start_server(self, body):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
        server.body, server.paths = body, []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.addCleanup(close)
        return server

    @staticmethod
    def origin(server):
        return f"http://127.0.0.1:{server.server_port}"

    def test_redirect_is_rejected_before_contacting_destination(self):
        with self.assertRaisesRegex(ValueError, "must not redirect"):
            replay.urlopen(self.origin(self.target) + "/redirect", timeout=2)
        self.assertEqual(self.target.paths, ["/redirect"])
        self.assertEqual(self.other.paths, [])

    def test_environment_proxy_cannot_reroute_loopback_request(self):
        proxy = self.origin(self.other)
        with patch.dict(os.environ, {"http_proxy": proxy, "HTTP_PROXY": proxy,
                                     "no_proxy": "", "NO_PROXY": ""}), \
             patch("urllib.request._opener", None):
            with replay.urlopen(self.origin(self.target) + "/metrics", timeout=2) as response:
                self.assertEqual(response.read(), b"local metrics")
        self.assertEqual(self.target.paths, ["/metrics"])
        self.assertEqual(self.other.paths, [])


if __name__ == "__main__":
    unittest.main()
