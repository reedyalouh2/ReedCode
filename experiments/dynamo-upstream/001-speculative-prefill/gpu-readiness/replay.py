"""Replay one frozen condition through a loopback token-input completion endpoint."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import socket
import sys
import time
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from prepare import REPO, ROOT, json_bytes, sha
from verify import read_gzip, verify


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise ValueError("Loopback endpoints must not redirect")


# A redirect or environment proxy would bypass the loopback URL check.
urlopen = build_opener(ProxyHandler({}), NoRedirect()).open


def selected(plan, condition):
    return [row for row in plan["sequence"] if row["kind"] == "real" or row["condition"] == condition]


def usage_counts(usage, prompt_tokens):
    if not isinstance(usage, dict) or usage.get("prompt_tokens") != prompt_tokens:
        raise ValueError("Final prompt usage differs from the frozen token input")
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    if type(cached) is not int or not 0 <= cached <= prompt_tokens:
        raise ValueError("Final cached-token usage is unavailable or invalid")
    return cached


def observe(metrics_url, identity_url, output, prefix):
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from metrics_identity import validate_identity

    for url in (metrics_url, identity_url):
        parsed = urlparse(url)
        if parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1", "::1") or parsed.username or parsed.password or parsed.query:
            raise ValueError("Metrics and identity must use explicit credential-free loopback HTTP URLs")
    challenge = secrets.token_hex(16)
    urls = {"metrics": metrics_url, "identity": identity_url + "?challenge=" + challenge}
    result = {"started_unix": time.time(), "files": {}}
    for kind, url in urls.items():
        with urlopen(Request(url, headers={"Cache-Control": "no-cache"}), timeout=5) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("Observation endpoint exceeded the capture limit")
        name = prefix + (".metrics.txt" if kind == "metrics" else ".identity.json")
        (output / name).write_bytes(raw)
        result["files"][name] = sha(raw)
        if kind == "identity":
            result["identity_fingerprint"] = validate_identity(json.loads(raw), challenge)
    result["ended_unix"] = time.time()
    return result


def send(url, row, request_id, output, timeout):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in ("localhost", "127.0.0.1", "::1") or parsed.username or parsed.password:
        raise ValueError("Use an explicit credential-free loopback endpoint")
    body = json_bytes(row["payload"])
    request = Request(url, data=body, headers={"Content-Type": "application/json", "X-Request-ID": request_id}, method="POST")
    started = time.monotonic()
    started_unix = time.time()
    first_token = None
    usage = None
    response_id = None
    finish_reason = None
    done = False
    with (output / (request_id + ".sse")).open("wb") as raw:
        with urlopen(request, timeout=timeout) as response:
            for line in response:
                if time.monotonic() - started > timeout:
                    raise TimeoutError("Request exceeded its trial deadline")
                raw.write(line)
                raw.flush()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    done = True
                    continue
                if not data:
                    continue
                item = json.loads(data)
                if "error" in item:
                    raise ValueError("Server returned an error frame")
                response_id = item.get("id", response_id)
                if item.get("usage") is not None:
                    usage = item["usage"]
                for choice in item.get("choices", []):
                    if choice.get("text") and first_token is None:
                        first_token = time.monotonic()
                    finish_reason = choice.get("finish_reason") or finish_reason
                if time.monotonic() - started > timeout:
                    raise TimeoutError("Request exceeded its trial deadline")
    if not done or finish_reason is None:
        raise ValueError("Completion stream did not finish normally")
    cached = usage_counts(usage, len(row["payload"]["prompt"]))
    ended = time.monotonic()
    return {"request_id": request_id, "response_id": response_id, "finish_reason": finish_reason,
            "started_unix": started_unix, "ended_unix": time.time(),
            "prompt_tokens": len(row["payload"]["prompt"]), "cached_tokens": cached,
            "ttft_seconds": None if first_token is None else first_token - started,
            "completion_seconds": ended - started, "usage": usage,
            "raw_sha256": sha((output / (request_id + ".sse")).read_bytes())}, ended


def run(args):
    verification = verify(args.directory)
    plan = read_gzip(args.directory / f"{args.session}-replay.json.gz")
    rows = selected(plan, args.condition)
    summary = {"session": args.session, "condition": args.condition, "repeat": args.repeat,
               "requests": len(rows), "prompt_tokens": sum(len(row["payload"]["prompt"]) for row in rows),
               "verification": verification, "execute": args.execute,
               "method": "controlled builder replay; original assistant decode KV is absent",
               "limitation": "Added fixed-warmup reuse can exceed the benefit in a live agent trajectory"}
    if not args.execute:
        print(json.dumps(summary, indent=2))
        return
    parsed = urlparse(args.base_url)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in ("localhost", "127.0.0.1", "::1") or parsed.username or parsed.password:
        raise ValueError("Use an explicit credential-free loopback tunnel endpoint")
    if not args.reset_record or not args.output:
        raise ValueError("Execution requires a verified reset record and a new output directory")
    reset = json.loads(args.reset_record.read_text())
    required = ("backend_cache_empty", "router_index_empty", "token_capture_ready", "kv_event_capture_ready")
    if any(reset.get(key) is not True for key in required) or not reset.get("process_epoch"):
        raise ValueError("External reset/capture verification is incomplete")
    if args.output.exists():
        raise ValueError("Choose a new output directory")
    args.output.mkdir(parents=True)
    summary.update({"started_at_utc": datetime.now(timezone.utc).isoformat(),
                    "hostname": socket.gethostname(),
                    "reset_record_sha256": sha(args.reset_record.read_bytes()), "reset_record": reset,
                    "transport": "frozen token-input completions; no live hint or transcript generation"})
    (args.output / "run.json").write_bytes(json_bytes(summary))
    started = time.monotonic()
    deadline = started + args.max_seconds
    last_real_end = None
    try:
        with (args.output / "requests.jsonl").open("w") as log:
            for index, row in enumerate(rows):
                if row["kind"] == "real" and last_real_end is not None:
                    due = last_real_end + row["recorded_tool_wait_ms"] / 1000
                    delay = max(0, due - time.monotonic())
                    if time.monotonic() + delay >= deadline:
                        raise TimeoutError("Trial deadline reached before tool wait completed")
                    time.sleep(delay)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Trial deadline reached")
                request_id = f"prefill-{args.session}-{args.condition}-r{args.repeat}-{index:02}"
                (args.output / (request_id + ".request.json")).write_bytes(json_bytes(row["payload"]))
                before = observe(args.metrics_url, args.identity_url, args.output, request_id + "-before") if getattr(args, "metrics_url", None) else None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Trial deadline reached during observation")
                measured, ended = send(args.base_url.rstrip("/") + "/completions", row, request_id, args.output, min(remaining, 120))
                after, observation_error = None, None
                if before:
                    try:
                        after = observe(args.metrics_url, args.identity_url, args.output, request_id + "-after")
                        if before["identity_fingerprint"] != after["identity_fingerprint"]:
                            raise ValueError("Backend identity changed during the request")
                    except Exception as error:
                        observation_error = str(error)
                record = {**{k: v for k, v in row.items() if k != "payload"}, **measured,
                          "start_offset_seconds": ended - started - measured["completion_seconds"],
                          "observations": {"before": before, "after": after, "error": observation_error}}
                log.write(json.dumps(record) + "\n")
                log.flush()
                if observation_error:
                    raise ValueError(observation_error)
                if index == 0 and measured["cached_tokens"] != 0:
                    raise ValueError("First request was not cold; reset failed")
                if row["kind"] == "real":
                    last_real_end = ended
        summary.update({"status": "http_replay_completed", "elapsed_seconds": time.monotonic() - started})
    except Exception as error:
        summary.update({"status": "failed", "error_type": type(error).__name__, "error": str(error),
                        "elapsed_seconds": time.monotonic() - started})
        raise
    finally:
        summary["files"] = {path.name: sha(path.read_bytes()) for path in sorted(args.output.iterdir()) if path.name != "run.json"}
        (args.output / "run.json").write_bytes(json_bytes(summary))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / "fixtures")
    parser.add_argument("--session", choices=("short", "long"), required=True)
    parser.add_argument("--condition", choices=("off", "stock", "fixed"), required=True)
    parser.add_argument("--repeat", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--base-url", default="http://127.0.0.1:18002/v1")
    parser.add_argument("--max-seconds", type=float, default=120)
    parser.add_argument("--reset-record", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--metrics-url")
    parser.add_argument("--identity-url")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not 0 < args.max_seconds <= 600:
        parser.error("max-seconds must be in (0, 600]")
    if bool(args.metrics_url) != bool(args.identity_url):
        parser.error("Enable metrics and identity snapshots together")
    run(args)
