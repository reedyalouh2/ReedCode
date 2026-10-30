import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

import httpx

from http_capture import HttpCapture, IncompleteStreamError, ServerStreamError


class Stream(httpx.AsyncByteStream):
    def __init__(self, blocked=False):
        self.blocked = blocked
        self.started = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        yield b'data: {"id":"server-1","choices":[{"text":"hello"}]}\n\n'
        self.started.set()
        if self.blocked:
            await asyncio.sleep(10)
        yield b'data: {"choices":[],"usage":{"prompt_tokens":16,"completion_tokens":1}}\n\n'
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.closed = True


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_pinned_dynamo_context_header_matches_saved_identity(self):
        sent = {}

        def handle(request):
            sent.update(request.headers)
            return httpx.Response(200, stream=Stream())

        with tempfile.TemporaryDirectory() as directory:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handle)
            ) as client:
                capture = HttpCapture(
                    "http://localhost:8000", "model", directory,
                    client=client, dynamo_request_id=True,
                )
                result = await capture("prepare", {"prompt": [1] * 16})
                await capture.close()
                identity = result["client_request_id"]
                self.assertEqual(str(uuid.UUID(identity)), identity)
                self.assertEqual(sent["x-request-id"], identity)
                self.assertEqual(sent["x-dynamo-request-id"], identity)
                self.assertNotIn("request-id", sent)
                saved = json.loads(Path(result["capture_file"]).read_text())
                self.assertEqual(
                    saved["request_headers"]["x-dynamo-request-id"], identity
                )

    async def test_exact_ids_and_completed_capture(self):
        stream = Stream()

        def handle(request):
            body = json.loads(request.content)
            self.assertEqual(body["prompt"], [151644, 198, 42])
            self.assertEqual(body["max_tokens"], 1)
            self.assertEqual(body["nvext"]["cache_salt"], "trial-1")
            return httpx.Response(200, stream=stream)

        with tempfile.TemporaryDirectory() as directory:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handle)
            ) as client:
                capture = HttpCapture(
                    "http://localhost:8000",
                    "model",
                    directory,
                    client=client,
                    cache_salt="trial-1",
                )
                got = await capture(
                    "prepare", {"prompt": [151644, 198, 42], "max_tokens": 1}
                )
                self.assertEqual(got["status"], "completed")
                self.assertEqual(got["usage"]["completion_tokens"], 1)
                await capture.close()
                self.assertTrue(Path(got["capture_file"]).is_file())
                self.assertNotIn("ttft", got)
        self.assertTrue(stream.closed)

    async def test_cancelled_request_keeps_partial_capture_and_closes_stream(self):
        stream = Stream(blocked=True)
        with tempfile.TemporaryDirectory() as directory:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, stream=stream)
                )
            ) as client:
                capture = HttpCapture(
                    "http://localhost:8000", "model", directory, client=client
                )
                task = asyncio.create_task(capture("prepare", {"prompt": [1] * 16}))
                await stream.started.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await capture.close()
                record = capture.records["prepare"]
                saved = json.loads(Path(record["capture_file"]).read_text())
                self.assertEqual(saved["status"], "cancelled")
                self.assertEqual(saved["server_response_id"], "server-1")
                self.assertGreater(len(saved["chunks"]), 0)
        self.assertTrue(stream.closed)

    async def test_remote_endpoint_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                HttpCapture("https://example.org", "model", directory)

    async def test_error_frame_is_saved_as_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200, content=b'data: {"error":{"message":"failed"}}\n\n'
                    )
                )
            ) as client:
                capture = HttpCapture(
                    "http://localhost:8000", "model", directory, client=client
                )
                with self.assertRaises(ServerStreamError):
                    await capture("prepare", {"prompt": [1]})
                await capture.close()
                self.assertEqual(capture.records["prepare"]["status"], "error")
                self.assertTrue(
                    Path(capture.records["prepare"]["capture_file"]).is_file()
                )

    async def test_early_eof_is_saved_as_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200, content=b'data: {"choices":[{"text":"partial"}]}\n\n'
                    )
                )
            ) as client:
                capture = HttpCapture(
                    "http://localhost:8000", "model", directory, client=client
                )
                with self.assertRaises(IncompleteStreamError):
                    await capture("prepare", {"prompt": [1]})
                await capture.close()
                self.assertEqual(
                    capture.records["prepare"]["error_type"], "IncompleteStreamError"
                )
                self.assertGreater(len(capture.records["prepare"]["chunks"]), 0)

    async def test_done_finishes_without_waiting_for_connection_eof(self):
        class DoneThenOpen(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                yield b"data: [DONE]\n\n"
                await asyncio.sleep(10)

            async def aclose(self):
                self.closed = True

        stream = DoneThenOpen()
        with tempfile.TemporaryDirectory() as directory:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, stream=stream)
                )
            ) as client:
                capture = HttpCapture(
                    "http://localhost:8000", "model", directory, client=client
                )
                result = await asyncio.wait_for(capture("target", {"prompt": [1]}), 0.5)
                self.assertEqual(result["status"], "completed")
                await capture.close()
        self.assertTrue(stream.closed)


    async def test_network_completion_does_not_wait_for_capture_flush(self):
        write_started = threading.Event()
        release_write = threading.Event()
        original = Path.write_text

        def delayed_write(path, data, *args, **kwargs):
            write_started.set()
            release_write.wait(timeout=0.5)
            return original(path, data, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, content=b"data: [DONE]\n\n")
                )
            ) as client:
                capture = HttpCapture(
                    "http://localhost:8000", "model", directory, client=client
                )
                with patch.object(Path, "write_text", delayed_write):
                    task = asyncio.create_task(capture("prepare", {"prompt": [1]}))
                    self.assertTrue(await asyncio.to_thread(write_started.wait, 0.5))
                    result = await task
                    self.assertEqual(result["status"], "completed")
                    pending = await capture.flush(timeout=0.01)
                    self.assertEqual(pending["prepare"]["status"], "pending")
                    release_write.set()
                    flushed = await capture.close()
                    self.assertEqual(flushed["prepare"]["status"], "written")
                saved = json.loads(
                    Path(capture.records["prepare"]["capture_file"]).read_text()
                )
                self.assertEqual(saved["status"], "completed")

    async def test_capture_write_failure_is_reported_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, content=b"data: [DONE]\n\n")
                )
            ) as client:
                capture = HttpCapture(
                    "http://localhost:8000", "model", directory, client=client
                )
                with patch.object(
                    Path, "write_text", side_effect=OSError("disk failure")
                ):
                    result = await capture("target", {"prompt": [1]})
                    flush = await capture.close()
                self.assertEqual(result["status"], "completed")
                self.assertEqual(
                    flush["target"], {"status": "error", "error_type": "OSError"}
                )


if __name__ == "__main__":
    unittest.main()
