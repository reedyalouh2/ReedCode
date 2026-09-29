#!/usr/bin/env python3
"""Forward a container's loopback port to the host's study SSH tunnel."""

import argparse
import select
import socket
import socketserver
import time


class RelayHandler(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.create_connection(("host.docker.internal", self.server.host_port), timeout=10) as remote:
            self.request.settimeout(10)
            remote.settimeout(10)
            peers = {self.request: remote, remote: self.request}
            deadline = time.monotonic() + 1500
            while peers:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                readable, _, _ = select.select(list(peers), [], [], remaining)
                for source in readable:
                    data = source.recv(65536)
                    destination = peers[source]
                    if data:
                        destination.sendall(data)
                    else:
                        destination.shutdown(socket.SHUT_WR)
                        del peers[source]


class RelayServer(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def __init__(self, port, host_port):
        self.host_port = host_port
        super().__init__(("127.0.0.1", port), RelayHandler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18000)
    parser.add_argument("--host-port", type=int, required=True)
    args = parser.parse_args()
    with RelayServer(args.port, args.host_port) as server:
        server.serve_forever(poll_interval=0.2)


if __name__ == "__main__":
    main()
