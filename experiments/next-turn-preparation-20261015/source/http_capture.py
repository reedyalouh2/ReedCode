"""Exact-token completion transport for a local Dynamo experiment endpoint."""

import asyncio
import json
from pathlib import Path
import time
from urllib.parse import urlsplit
import uuid

import httpx


class IncompleteStreamError(Exception):
    pass


class ServerStreamError(Exception):
    pass


def _consume_write_result(task):
    if not task.cancelled():
        task.exception()


def _write_capture(path, record):
    path.write_text(json.dumps(record, indent=2) + "\n")


class HttpCapture:
    def __init__(
        self, base_url, model, directory, *, client=None, cache_salt=None,
        dynamo_request_id=False,
    ):
        address = urlsplit(base_url)
        if address.scheme != "http" or address.hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError("Use a local endpoint or SSH tunnel")
        self.url = base_url.rstrip("/") + "/v1/completions"
        self.model = model
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.client = client or httpx.AsyncClient(timeout=60)
        self.owns_client = client is None
        self.cache_salt = cache_salt
        self.dynamo_request_id = dynamo_request_id
        self.records = {}
        self.writes = {}

    async def close(self):
        writes = await self.flush()
        if self.owns_client:
            await self.client.aclose()
        return writes

    async def flush(self, timeout=2.0):
        """Call after request cleanup; pending or failed writes invalidate capture."""
        if self.writes:
            await asyncio.wait(tuple(self.writes.values()), timeout=timeout)
        results = {}
        for label, task in self.writes.items():
            if not task.done():
                results[label] = {"status": "pending"}
            elif task.cancelled():
                results[label] = {"status": "cancelled"}
            elif task.exception() is not None:
                results[label] = {
                    "status": "error",
                    "error_type": type(task.exception()).__name__,
                }
            else:
                results[label] = {"status": "written"}
        return results

    async def __call__(self, label, payload):
        tokens = payload["prompt"]
        if not tokens or any(type(token) is not int or token < 0 for token in tokens):
            raise ValueError("Prompt must contain exact non-negative token IDs")
        if label in self.records:
            raise ValueError("Each request needs a unique capture label")
        request_uuid = uuid.uuid4()
        request_id = str(request_uuid) if self.dynamo_request_id else request_uuid.hex
        headers = {"X-Request-ID": request_id}
        if self.dynamo_request_id:
            # Dynamo 1.5 reads this deprecated header into its engine context ID.
            headers["x-dynamo-request-id"] = request_id
        body = {
            "model": self.model,
            "prompt": list(tokens),
            "max_tokens": payload.get("max_tokens", 32),
            "temperature": 0,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self.cache_salt is not None:
            body["nvext"] = {"cache_salt": self.cache_salt}
        started = time.perf_counter()
        record = {
            "label": label,
            "client_request_id": request_id,
            "request_headers": headers,
            "request": body,
            "chunks": [],
        }
        self.records[label] = record
        try:
            async with self.client.stream(
                "POST", self.url, json=body, headers=headers
            ) as response:
                record["http_status"] = response.status_code
                response.raise_for_status()
                terminal = False
                async for line in response.aiter_lines():
                    now = time.perf_counter() - started
                    record["chunks"].append({"seconds": now, "line": line})
                    if not line.startswith("data:"):
                        continue
                    if line[5:].strip() == "[DONE]":
                        terminal = True
                        break
                    try:
                        chunk = json.loads(line[5:])
                    except json.JSONDecodeError:
                        continue
                    if isinstance(chunk, dict):
                        if "error" in chunk:
                            raise ServerStreamError("Dynamo returned a stream error")
                        if chunk.get("id"):
                            record["server_response_id"] = chunk["id"]
                        if chunk.get("usage") is not None:
                            record["usage"] = chunk["usage"]
                        if any(
                            choice.get("text") for choice in chunk.get("choices", [])
                        ):
                            record.setdefault("first_visible_text_seconds", now)
                if not terminal:
                    raise IncompleteStreamError(
                        "Completion stream ended without [DONE]"
                    )
                record["status"] = "completed"
        except asyncio.CancelledError:
            record["status"] = "cancelled"
            raise
        except Exception as error:
            record["status"] = "error"
            record["error_type"] = type(error).__name__
            raise
        finally:
            record["end_seconds"] = time.perf_counter() - started
            record["capture_file"] = str(self.directory / f"{request_id}.json")
            # Disk completion must not change a measured network deadline.
            task = asyncio.create_task(
                asyncio.to_thread(_write_capture, Path(record["capture_file"]), record)
            )
            task.add_done_callback(_consume_write_result)
            self.writes[label] = task
        return {
            key: value
            for key, value in record.items()
            if key not in {"request", "chunks"}
        }
