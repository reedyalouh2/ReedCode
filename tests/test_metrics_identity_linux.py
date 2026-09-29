import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from metrics_identity import ProcessMonitor, handler_for, validate_identity


@unittest.skipUnless(sys.platform == "linux", "Requires Linux /proc")
class LiveProcessIdentityTests(unittest.TestCase):
    def test_engine_exit_invalidates_live_http_identity(self):
        code = '''
import subprocess, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
    def log_message(self, *args): pass
server = HTTPServer(('127.0.0.1', 0), Handler)
print(child.pid, server.server_port, flush=True)
server.serve_forever()
'''
        backend = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        server = None
        engine = None
        try:
            engine, metrics_port = map(int, backend.stdout.readline().split())
            monitor = ProcessMonitor(backend.pid, [engine], metrics_port)
            observed = monitor.snapshot()
            self.assertEqual({p["pid"] for p in observed["processes"]}, {backend.pid, engine})
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(monitor))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            challenge = "a" * 32
            url = f"http://127.0.0.1:{server.server_port}/identity?challenge={challenge}"
            with urllib.request.urlopen(url, timeout=2) as response:
                body = json.load(response)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(len(validate_identity(body, challenge)), 64)
            os.kill(engine, signal.SIGTERM)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(url, timeout=2):
                        pass
                except urllib.error.HTTPError as error:
                    self.assertEqual(error.code, 503)
                    break
                time.sleep(0.01)
            else:
                self.fail("Engine exit did not invalidate identity")
        finally:
            if server:
                server.shutdown()
                server.server_close()
            backend.terminate()
            backend.wait(timeout=5)
            backend.stdout.close()
            if engine is not None:
                try:
                    os.kill(engine, signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    unittest.main()
