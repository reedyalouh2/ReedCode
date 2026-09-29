#!/usr/bin/env python3
"""Capture HTTP entity bytes between a coding client and a loopback server."""

import argparse
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import socket
import threading
import time
import uuid
from urllib.parse import urlsplit


HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade"}
SAFE_HEADERS = {"accept", "content-type", "content-encoding", "content-length",
                "user-agent", "anthropic-version", "anthropic-beta", "openai-beta",
                "x-request-id", "request-id", "session_id", "originator"}


def safe_headers(headers):
    return [[key, value] for key, value in headers
            if key.lower() in SAFE_HEADERS or key.lower().startswith("x-claude-code-")]


def inference_request(method, path):
    route = urlsplit(path).path.rstrip("/")
    return method == "POST" and (route == "/v1/messages" or route.startswith("/v1/responses"))


class CaptureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, upstream, output, max_calls=15, seconds=1500, trace_id_prefix=None):
        parsed = urlsplit(upstream)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path not in ("", "/"):
            raise ValueError("upstream must be an HTTP origin on 127.0.0.1")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("upstream must not contain credentials, a query, or a fragment")
        if address[0] != "127.0.0.1" or max_calls < 1 or seconds <= 0:
            raise ValueError("use loopback with positive call and time limits")
        self.upstream_port = parsed.port or 80
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=False)
        self.max_calls, self.seconds = max_calls, seconds
        if trace_id_prefix is not None and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", trace_id_prefix):
            raise ValueError("trace ID prefix must be 1–64 letters, digits, dots, underscores or hyphens")
        self.trace_id_prefix = trace_id_prefix
        self.lock = threading.Lock()
        self.active = set()
        self.calls = self.sequence = 0
        self.first_inference = None
        super().__init__(address, CaptureHandler)

    def cancel_active(self):
        with self.lock:
            handlers = list(self.active)
        for handler in handlers:
            handler.cancelled = True
            for connection in (handler.connection, handler.upstream_socket):
                if connection:
                    try:
                        connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

    def admit(self, is_inference):
        with self.lock:
            self.sequence += 1
            now = time.monotonic()
            if is_inference and self.first_inference is None:
                self.first_inference = now
            deadline = (self.first_inference or now) + self.seconds
            allowed = now < deadline and (not is_inference or self.calls < self.max_calls)
            if allowed and is_inference:
                self.calls += 1
            return self.sequence, self.calls, deadline, allowed


class CaptureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def _body(self):
        lengths = self.headers.get_all("Content-Length", [])
        transfer = self.headers.get("Transfer-Encoding", "").lower()
        if len(lengths) > 1 or (lengths and transfer):
            raise ValueError("ambiguous request framing")
        if transfer:
            if transfer != "chunked":
                raise ValueError("unsupported transfer encoding")
            pieces = []
            total = 0
            while True:
                size = int(self.rfile.readline(100).split(b";", 1)[0].strip(), 16)
                if size < 0 or total + size > 8 * 1024 * 1024:
                    raise ValueError("request body exceeds capture limit")
                if size == 0:
                    if self.rfile.readline(8192) != b"\r\n":
                        raise ValueError("request trailers are unsupported")
                    break
                chunk = self.rfile.read(size)
                if len(chunk) != size or self.rfile.read(2) != b"\r\n":
                    raise ValueError("incomplete chunk")
                pieces.append(chunk)
                total += size
            return b"".join(pieces)
        size = int(lengths[0]) if lengths else 0
        if size < 0 or size > 8 * 1024 * 1024:
            raise ValueError("request body exceeds capture limit")
        result = self.rfile.read(size)
        if len(result) != size:
            raise ValueError("incomplete request body")
        return result

    def _reply(self, status, message):
        body = json.dumps({"error": {"type": "capture_proxy_error", "message": message}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def _handle(self):
        self.cancelled, self.upstream_socket = False, None
        with self.server.lock:
            self.server.active.add(self)
        started = time.monotonic_ns()
        is_inference = inference_request(self.command, self.path)
        seq, calls, deadline, allowed = self.server.admit(is_inference)
        folder = self.server.output / f"{seq:04d}"
        folder.mkdir()
        auth_values = self.headers.get_all("Authorization", [])
        auth = auth_values[0] if len(auth_values) == 1 else None
        forbidden_auth = len(auth_values) > 1 or auth not in (None, "Bearer local-dynamo") or any(
            self.headers.get(name) is not None for name in ("x-api-key", "cookie", "proxy-authorization")
        )
        original_trace_ids = self.headers.get_all("X-Request-ID", [])
        observer_trace_id = (self.server.trace_id_prefix + "-" + uuid.uuid4().hex
                             if self.server.trace_id_prefix and not original_trace_ids else None)
        meta = {"sequence": seq, "method": self.command, "path": self.path,
                "inference": is_inference, "admitted_inference_count": calls,
                "started_unix_ns": time.time_ns(), "started_monotonic_ns": started,
                "request_headers": safe_headers(self.headers.items()),
                "original_x_request_ids": original_trace_ids,
                "observer_x_request_id": observer_trace_id,
                "injected_request_headers": [["X-Request-ID", observer_trace_id]] if observer_trace_id else [],
                "authorization_present": bool(auth_values),
                "authorization_is_study_dummy": auth == "Bearer local-dynamo",
                "unexpected_credentials": forbidden_auth, "complete": False, "chunks": []}
        upstream = None
        response_started = False
        response_hash = hashlib.sha256()
        try:
            if forbidden_auth:
                meta["error"] = "unexpected credentials; request body not read or forwarded"
                self._reply(403, "unexpected credentials")
                return
            if not allowed:
                meta["error"] = "session call or time limit"
                self._reply(429, "session call or time limit")
                return
            self.connection.settimeout(max(0.01, deadline - time.monotonic()))
            body = self._body()
            (folder / "request.body").write_bytes(body)
            meta["request_sha256"] = hashlib.sha256(body).hexdigest()
            meta["request_bytes"] = len(body)
            upstream = http.client.HTTPConnection("127.0.0.1", self.server.upstream_port,
                                                 timeout=max(0.01, deadline - time.monotonic()))
            connection_headers = {name.strip().lower() for value in self.headers.get_all("Connection", [])
                                  for name in value.split(",")}
            upstream.putrequest(self.command, self.path, skip_accept_encoding=True)
            for key, value in self.headers.items():
                if key.lower() not in HOP_HEADERS | connection_headers | {"host", "content-length"}:
                    upstream.putheader(key, value)
            upstream.putheader("Content-Length", str(len(body)))
            if observer_trace_id:
                upstream.putheader("X-Request-ID", observer_trace_id)
            upstream.putheader("Connection", "close")
            upstream.endheaders(body)
            response_socket = upstream.sock
            self.upstream_socket = response_socket
            response = upstream.getresponse()
            meta["response_status"] = response.status
            meta["response_headers"] = safe_headers(response.getheaders())
            meta["response_headers_monotonic_ns"] = time.monotonic_ns()
            self.send_response_only(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() not in HOP_HEADERS:
                    self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            response_started = True
            with (folder / "response.body").open("wb") as output:
                while self.command != "HEAD":
                    if response.isclosed():
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("session time limit")
                    if response_socket:
                        response_socket.settimeout(remaining)
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    received = time.monotonic_ns()
                    output.write(chunk)
                    output.flush()
                    response_hash.update(chunk)
                    meta["chunks"].append({"received_monotonic_ns": received, "bytes": len(chunk)})
                    self.wfile.write(chunk)
                    self.wfile.flush()
            if self.command != "HEAD" and response.length not in (None, 0):
                raise http.client.IncompleteRead(b"", response.length)
            if self.cancelled:
                raise InterruptedError("capture controller stopped request")
            meta["complete"] = True
        except (OSError, ValueError, http.client.HTTPException) as error:
            meta["error"] = type(error).__name__ + ": " + str(error)
            if not response_started:
                try:
                    self._reply(502, "upstream or capture failure")
                except OSError:
                    pass
        finally:
            if upstream:
                upstream.close()
            meta["response_sha256"] = response_hash.hexdigest()
            meta["response_bytes"] = sum(chunk["bytes"] for chunk in meta["chunks"])
            meta["ended_monotonic_ns"] = time.monotonic_ns()
            metadata_path = folder / "metadata.json.tmp"
            metadata_path.write_text(json.dumps(meta, indent=2) + "\n")
            metadata_path.replace(folder / "metadata.json")
            with self.server.lock:
                self.server.active.discard(self)
            self.close_connection = True

    do_GET = do_POST = do_HEAD = _handle


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18001)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-calls", type=int, default=15)
    parser.add_argument("--seconds", type=float, default=1500)
    parser.add_argument("--trace-id-prefix")
    args = parser.parse_args()
    server = CaptureServer(("127.0.0.1", args.port), args.upstream, args.output,
                           args.max_calls, args.seconds, args.trace_id_prefix)
    print(json.dumps({"port": server.server_port, "upstream": args.upstream}), flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
