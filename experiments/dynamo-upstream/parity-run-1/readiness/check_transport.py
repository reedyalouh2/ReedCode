#!/usr/bin/env python3
"""Check Docker-to-host loopback transport using a local fixed HTTP response."""

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import threading
import time


REQUEST = b'{"local_transport_check":true,"whitespace":  "kept"}\n'
RESPONSE = b'data: {"local_transport_check":true}\n\ndata: [DONE]\n\n'


class LocalHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.received.append({"path": self.path, "request_sha256": hashlib.sha256(body).hexdigest(),
                                     "body_matches": body == REQUEST,
                                     "authorization_present": self.headers.get("Authorization") is not None})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(RESPONSE)))
        self.end_headers()
        self.wfile.write(RESPONSE[:20])
        self.wfile.flush()
        time.sleep(0.2)
        self.wfile.write(RESPONSE[20:])
        self.wfile.flush()


CONTAINER_CHECK = r'''
import hashlib, http.client, json, sys, threading, time
from pathlib import Path
from capture_proxy import CaptureServer
from transport_relay import RelayServer
relay = RelayServer(0, int(sys.argv[1]))
proxy = CaptureServer(("127.0.0.1", 0), f"http://127.0.0.1:{relay.server_address[1]}", Path("/tmp/capture"), 15, 15)
for server in (relay, proxy):
    threading.Thread(target=server.serve_forever, daemon=True).start()
body = b'{"local_transport_check":true,"whitespace":  "kept"}\n'
client = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=10)
client.request("POST", "/v1/responses", body, {"Content-Type": "application/json"})
response = client.getresponse()
captured = response.read()
client.close()
for server in (proxy, relay):
    server.shutdown()
    server.server_close()
meta = json.loads(Path("/tmp/capture/0001/metadata.json").read_text())
print(json.dumps({"response_status": response.status, "response_sha256": hashlib.sha256(captured).hexdigest(),
                  "metadata": meta, "response_body": captured.decode(),
                  "request_body": Path("/tmp/capture/0001/request.body").read_text()}))
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--docker", default="/usr/local/bin/docker")
    parser.add_argument("--image", default="reedcode-codex-stub-native:0.155.1")
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("output already exists")
    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalHandler)
    server.received = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    readiness = Path(__file__).resolve().parent
    command = [args.docker, "run", "--rm", "--network", "bridge", "--platform", "linux/arm64",
               "--tmpfs", "/root", "--tmpfs", "/tmp", "-v", f"{readiness}:/readiness:ro",
               "-w", "/readiness", args.image, "python3", "-c", CONTAINER_CHECK, str(server.server_port)]
    try:
        process = subprocess.run(command, capture_output=True, text=True, timeout=30)
    finally:
        server.shutdown()
        server.server_close()
    result = {"kind": "local Docker bridge transport check", "model_requests_made": 0,
              "gpu_requests_made": 0, "host_bind": "127.0.0.1", "exit_code": process.returncode,
              "command": command, "host_received": server.received, "stderr": process.stderr}
    try:
        result["container"] = json.loads(process.stdout)
    except json.JSONDecodeError:
        result["stdout"] = process.stdout
    observed = result.get("container", {})
    result["passed"] = (process.returncode == 0 and len(server.received) == 1
                        and server.received[0]["body_matches"] and not server.received[0]["authorization_present"]
                        and observed.get("response_status") == 200
                        and observed.get("response_sha256") == hashlib.sha256(RESPONSE).hexdigest()
                        and observed.get("metadata", {}).get("complete") is True
                        and observed.get("request_body", "").encode() == REQUEST
                        and len(observed.get("metadata", {}).get("chunks", [])) >= 2)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in ("kind", "exit_code", "passed", "host_received")}))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
