"""Serve restart identity for a single Linux backend and its engine processes."""

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit
import uuid


class IdentityUnavailable(ValueError):
    pass


def read_process(proc_root, pid):
    raw = (proc_root / str(pid) / "stat").read_text()
    fields = raw[raw.rfind(")") + 2:].split()
    if int(raw.split(" ", 1)[0]) != pid or len(fields) < 20 or fields[0] in {"Z", "X"}:
        raise IdentityUnavailable("Process unavailable")
    return {"pid": pid, "parent_pid": int(fields[1]), "start_ticks": int(fields[19])}


def process_tree(proc_root, backend_pid):
    processes = {}
    for path in proc_root.iterdir():
        if path.name.isdecimal():
            try:
                process = read_process(proc_root, int(path.name))
            except (OSError, ValueError, IndexError):
                continue
            processes[process["pid"]] = process
    if backend_pid not in processes:
        raise IdentityUnavailable("Backend unavailable")
    selected = {backend_pid}
    while True:
        children = {pid for pid, process in processes.items() if process["parent_pid"] in selected}
        expanded = selected | children
        if expanded == selected:
            return [processes[pid] for pid in sorted(selected)]
        selected = expanded


def socket_binding(proc_root, port, pids):
    listeners = set()
    for name in ("tcp", "tcp6"):
        path = proc_root / "net" / name
        if not path.exists():
            continue
        for line in path.read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) > 9 and fields[3] == "0A" and int(fields[1].rsplit(":", 1)[1], 16) == port:
                listeners.add(fields[9])
    owned = set()
    for pid in pids:
        for descriptor in (proc_root / str(pid) / "fd").iterdir():
            try:
                target = str(descriptor.readlink())
            except FileNotFoundError:
                continue
            match = re.fullmatch(r"socket:\[(\d+)\]", target)
            if match:
                owned.add(match[1])
    if not listeners or not listeners <= owned:
        raise IdentityUnavailable("Metrics listener is outside the watched process tree")
    return sorted(listeners)


class ProcessMonitor:
    """Pin a backend tree after loading the model; fail closed if it changes."""

    def __init__(self, backend_pid, engine_pids, metrics_port, proc_root=Path("/proc")):
        if backend_pid <= 0 or not engine_pids or any(pid <= 0 or pid == backend_pid for pid in engine_pids):
            raise ValueError("Provide a backend PID and at least one separate engine PID")
        if len(engine_pids) != len(set(engine_pids)) or not 1 <= metrics_port <= 65535:
            raise ValueError("Invalid engine PIDs or metrics port")
        self.proc_root = Path(proc_root)
        self.backend_pid = backend_pid
        self.engine_pids = sorted(engine_pids)
        self.metrics_port = metrics_port
        self.monitor_id = str(uuid.uuid4())
        self.pinned = self._observe()

    def _observe(self):
        tree = process_tree(self.proc_root, self.backend_pid)
        pids = {process["pid"] for process in tree}
        if not set(self.engine_pids) <= pids:
            raise IdentityUnavailable("Every declared engine must be a live backend descendant")
        boot_id = (self.proc_root / "sys/kernel/random/boot_id").read_text().strip()
        uuid.UUID(boot_id)
        return {"boot_id": boot_id, "processes": tree,
                "backend_pid": self.backend_pid, "engine_pids": self.engine_pids,
                "metrics_port": self.metrics_port,
                "metrics_socket_inodes": socket_binding(self.proc_root, self.metrics_port, pids)}

    def snapshot(self):
        observed = self._observe()
        if observed != self.pinned:
            raise IdentityUnavailable("Watched processes or metrics listener changed")
        return {"schema_version": 1, "monitor_id": self.monitor_id, **observed}


def validate_identity(body, challenge):
    """Validate the live sidecar response and return its stable fingerprint."""
    if not isinstance(body, dict) or body.get("challenge") != challenge or body.get("schema_version") != 1:
        raise IdentityUnavailable("Invalid identity response")
    try:
        uuid.UUID(body["boot_id"])
        uuid.UUID(body["monitor_id"])
        port = body["metrics_port"]
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError
        processes = body["processes"]
        if not isinstance(processes, list) or not processes:
            raise ValueError
        pids = set()
        for process in processes:
            if set(process) != {"pid", "parent_pid", "start_ticks"}:
                raise ValueError
            if any(type(value) is not int for value in process.values()):
                raise ValueError
            if process["pid"] <= 0 or process["parent_pid"] < 0 or process["start_ticks"] < 0 or process["pid"] in pids:
                raise ValueError
            pids.add(process["pid"])
        backend = body["backend_pid"]
        engines = body["engine_pids"]
        if type(backend) is not int or backend not in pids or not isinstance(engines, list) or not engines:
            raise ValueError
        if any(type(pid) is not int or pid not in pids or pid == backend for pid in engines):
            raise ValueError
        reachable = {backend}
        while True:
            expanded = reachable | {p["pid"] for p in processes if p["parent_pid"] in reachable}
            if expanded == reachable:
                break
            reachable = expanded
        if reachable != pids or len(set(engines)) != len(engines):
            raise ValueError
        inodes = body["metrics_socket_inodes"]
        if not isinstance(inodes, list) or not inodes or any(not isinstance(i, str) or not i.isdecimal() or int(i) <= 0 for i in inodes):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise IdentityUnavailable("Invalid identity response") from error
    identity = {key: body[key] for key in ("schema_version", "boot_id", "monitor_id", "backend_pid",
                                           "engine_pids", "processes", "metrics_port", "metrics_socket_inodes")}
    identity["processes"] = sorted(processes, key=lambda process: process["pid"])
    identity["engine_pids"] = sorted(engines)
    identity["metrics_socket_inodes"] = sorted(inodes)
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def handler_for(monitor):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = urlsplit(self.path)
            challenges = parse_qs(query.query).get("challenge", [])
            if query.path != "/identity" or len(challenges) != 1 or not re.fullmatch(r"[0-9a-f]{32}", challenges[0]):
                self.send_error(400)
                return
            try:
                body = {**monitor.snapshot(), "challenge": challenges[0]}
                status = 200
            except (OSError, ValueError, IndexError):
                body, status = {"error": "IdentityUnavailable"}, 503
            encoded = json.dumps(body, sort_keys=True).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args):
            pass
    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-pid", required=True, type=int)
    parser.add_argument("--engine-pid", required=True, action="append", type=int)
    parser.add_argument("--metrics-port", required=True, type=int)
    parser.add_argument("--port", type=int, default=9099)
    args = parser.parse_args(argv)
    monitor = ProcessMonitor(args.backend_pid, args.engine_pid, args.metrics_port)
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(monitor)) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
