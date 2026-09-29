"""Resume the one remaining public image layer in bounded registry chunks."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import time
import urllib.error
import urllib.parse
import urllib.request


DIGEST = "sha256:8fc125d30d846b6dcbf6d08c321091ef5b10b1dd42446f46b86d6e189b41f664"
SIZE = 4_634_646_584
ROOT = "https://ttl.sh/v2/reedcode-dynamo-92cfdea446c94719b36df5db02cc8189"
CHUNK = 64 * 1024 * 1024
DEADLINE = 900


class UploadError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@contextlib.contextmanager
def deadline(seconds):
    def expired(signum, frame):
        raise TimeoutError("request deadline")

    old = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def request(method, url, data=None, headers=None):
    # No credentials, proxies, redirects, or implicit write retries.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    hdr = {"User-Agent": "ReedCode-public-layer-upload/1", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=hdr, method=method)
    with deadline(DEADLINE):
        try:
            with opener.open(req, timeout=DEADLINE) as response:
                return response.status, dict(response.headers.items())
        except urllib.error.HTTPError as error:
            result = error.code, dict(error.headers.items())
            error.close()
            return result


def header(headers, name):
    return next((value for key, value in headers.items() if key.lower() == name.lower()), None)


def location(value, previous=ROOT + "/blobs/uploads/"):
    if not value:
        raise UploadError("registry omitted Location")
    value = urllib.parse.urljoin(previous, value)
    parsed = urllib.parse.urlsplit(value)
    expected = urllib.parse.urlsplit(ROOT)
    prefix = expected.path + "/blobs/uploads/"
    if (parsed.scheme, parsed.netloc) != (expected.scheme, expected.netloc):
        raise UploadError("registry Location changed origin")
    if not parsed.path.startswith(prefix) or not re.fullmatch(r"[A-Za-z0-9_-]+", parsed.path[len(prefix):]):
        raise UploadError("registry Location changed repository or upload path")
    if parsed.fragment:
        raise UploadError("unexpected Location fragment")
    return value


def save(path, state):
    state["updated_unix"] = time.time()
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as output:
        json.dump(state, output, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def verify_file(source, digest=DIGEST, size=SIZE):
    before = os.fstat(source.fileno())
    if before.st_size != size:
        raise UploadError("source size differs from pinned manifest")
    full = hashlib.sha256()
    source.seek(0)
    while block := source.read(CHUNK):
        full.update(block)
    after = os.fstat(source.fileno())
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise UploadError("source changed while hashing")
    if "sha256:" + full.hexdigest() != digest:
        raise UploadError("source SHA-256 differs from pinned manifest")
    source.seek(0)
    return identity


class Uploader:
    def __init__(self, source, checkpoint, transport=request, digest=DIGEST, size=SIZE, chunk=CHUNK):
        self.source, self.path, self.call = source, checkpoint, transport
        self.digest, self.size, self.chunk = digest, size, chunk
        self.identity = verify_file(source, digest, size)
        if checkpoint.exists():
            self.state = json.loads(checkpoint.read_text())
            if any(self.state.get(key) != value for key, value in
                   {"digest": digest, "size": size, "target": ROOT, "chunk_bytes": chunk}.items()):
                raise UploadError("checkpoint belongs to a different transfer")
            if self.state.get("upload_url"):
                location(self.state["upload_url"])
        else:
            self.state = {"digest": digest, "size": size, "target": ROOT,
                          "chunk_bytes": chunk, "offset": 0, "stage": "new", "events": []}

    def adopt_probe(self, path):
        if self.path.exists():
            return
        raw = path.read_bytes()
        probe = json.loads(raw)
        if probe.get("digest") != self.digest or probe.get("size") != self.size:
            raise UploadError("probe belongs to a different blob")
        patch, status = probe["patch"], probe["get"]
        value = header(status["headers"], "Range")
        match = re.fullmatch(r"0-(\d+)", value or "")
        if (patch["status"] != 202 or status["status"] != 204 or not match or
                int(match.group(1)) < 1 or header(patch["headers"], "Range") != value):
            raise UploadError("probe does not prove an accepted nonempty chunk")
        upload = location(probe["location"])
        if any(location(header(row["headers"], "Location"), upload) != upload for row in (patch, status)):
            raise UploadError("probe upload locations differ")
        offset = int(match.group(1)) + 1
        if offset > self.size:
            raise UploadError("probe exceeds blob size")
        self.state.update(upload_url=upload, offset=offset, pending=None, stage="uploading",
                          probe_sha256=hashlib.sha256(raw).hexdigest())
        self.record("probe_adopted", offset=offset)

    def record(self, event, **fields):
        row = {"event": event, "unix": time.time(), **fields}
        self.state["events"].append(row)
        save(self.path, self.state)
        print(json.dumps(row), flush=True)

    def exists(self):
        status, headers = self.call("HEAD", ROOT + "/blobs/" + self.digest)
        if status == 404:
            return False
        if status != 200:
            raise UploadError(f"blob HEAD returned HTTP {status}")
        if header(headers, "Content-Length") != str(self.size):
            raise UploadError("published blob size mismatch")
        if header(headers, "Docker-Content-Digest") != self.digest:
            raise UploadError("published blob digest header mismatch")
        self.state.update(stage="complete", offset=self.size, pending=None)
        self.record("complete", size=self.size, digest=self.digest)
        return True

    def reconcile(self):
        previous = self.state["upload_url"]
        status, headers = self.call("GET", previous)
        if status != 204:
            self.record("status_failed", status=status)
            raise UploadError(f"upload status HTTP {status}; stopped without creating another upload")
        current = location(header(headers, "Location"), previous)
        match = re.fullmatch(r"0-(\d+)", header(headers, "Range") or "")
        if not match:
            raise UploadError("missing or invalid upload Range")
        end = int(match.group(1))
        pending = self.state.get("pending")
        if end == 0:
            if pending or self.state["offset"] != 0:
                raise UploadError("Range 0-0 is ambiguous after a write; stopped")
            offset = 0
        else:
            offset = end + 1
        previous_offset = self.state["offset"]
        maximum = pending["end"] + 1 if pending else previous_offset
        if not previous_offset <= offset <= min(maximum, self.size):
            raise UploadError("server offset moved outside the recorded write range")
        self.state.update(upload_url=current, offset=offset, pending=None, stage="uploading")
        self.record("reconciled", offset=offset)
        return offset

    def run(self, max_chunks=None):
        if self.exists():
            return
        if not self.state.get("upload_url"):
            if self.state["stage"] != "new":
                raise UploadError("previous POST outcome unknown; no automatic POST retry")
            self.state["stage"] = "post_pending"
            self.record("post_started")
            status, headers = self.call("POST", ROOT + "/blobs/uploads/", b"", {"Content-Length": "0"})
            if status != 202:
                raise UploadError(f"new upload HTTP {status}; no automatic POST retry")
            self.state.update(upload_url=location(header(headers, "Location")), stage="uploading")
            self.record("post_accepted")
        self.reconcile()
        completed, no_progress = 0, 0
        while self.state["offset"] < self.size:
            if max_chunks is not None and completed >= max_chunks:
                self.record("paused", offset=self.state["offset"])
                return
            stat = os.fstat(self.source.fileno())
            if self.identity != (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns):
                raise UploadError("source changed after verification")
            start = self.state["offset"]
            self.source.seek(start)
            data = self.source.read(min(self.chunk, self.size - start))
            if not data:
                raise UploadError("source ended unexpectedly")
            end = start + len(data) - 1
            self.state.update(stage="patch_pending", pending={"start": start, "end": end,
                              "sha256": hashlib.sha256(data).hexdigest()})
            self.record("patch_started", start=start, end=end)
            try:
                status, headers = self.call("PATCH", self.state["upload_url"], data,
                    {"Content-Type": "application/octet-stream", "Content-Length": str(len(data)),
                     "Content-Range": f"{start}-{end}"})
            except Exception as error:
                status, headers = None, {}
                self.record("patch_uncertain", error_type=type(error).__name__)
            else:
                self.record("patch_response", status=status)
            if status == 202:
                self.state["upload_url"] = location(header(headers, "Location"), self.state["upload_url"])
                if header(headers, "Range") != f"0-{end}":
                    raise UploadError("successful PATCH returned unexpected Range")
            offset = self.reconcile()
            if status == 202 and offset != end + 1:
                raise UploadError("GET progress contradicts accepted PATCH")
            no_progress = no_progress + 1 if offset == start else 0
            if status is not None and status not in (202, 416) and status < 500:
                raise UploadError(f"PATCH HTTP {status}; offset reconciled, stopped")
            if no_progress >= 3:
                raise UploadError("three reconciled writes made no progress; stopped")
            completed += 1
        self.state["stage"] = "finalize_pending"
        self.record("finalize_started")
        parts = urllib.parse.urlsplit(self.state["upload_url"])
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        query = [(key, value) for key, value in query if key != "digest"] + [("digest", self.digest)]
        final_url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
        try:
            status, _ = self.call("PUT", final_url, b"", {"Content-Length": "0"})
            self.record("finalize_response", status=status)
        except Exception as error:
            self.record("finalize_uncertain", error_type=type(error).__name__)
        if not self.exists():
            self.reconcile()
            raise UploadError("finalization not confirmed; checkpoint saved, stopped")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--probe", type=Path)
    parser.add_argument("--max-chunks", type=int)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.max_chunks is not None and args.max_chunks < 1:
        parser.error("--max-chunks must be positive")
    args.state.parent.mkdir(parents=True, exist_ok=True)
    try:
        with args.state.with_name(args.state.name + ".lock").open("a") as lock, args.file.open("rb") as source:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            uploader = Uploader(source, args.state)
            if args.execute:
                if args.probe:
                    uploader.adopt_probe(args.probe)
                uploader.run(args.max_chunks)
            else:
                print(json.dumps({"verified_sha256": DIGEST, "size": SIZE, "target": ROOT,
                                  "chunk_bytes": CHUNK, "request_deadline_seconds": DEADLINE,
                                  "execute": False}))
    except Exception as error:
        # Network exceptions can embed URLs; only our own fixed messages are printable.
        message = str(error) if isinstance(error, UploadError) else type(error).__name__
        print(json.dumps({"stopped": message}), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
