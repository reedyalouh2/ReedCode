import asyncio
import json
import threading
import time
import unittest

from capture_worker import Capture, install_capture
import test_capture_worker


class GateStream:
    def __init__(self, *, fail=False):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.records = []
        self.fail = fail

    def write(self, text):
        self.entered.set()
        self.release.wait()
        if self.fail:
            raise OSError("disk unavailable")
        self.records.append(text)

    def flush(self):
        pass

    def close(self):
        pass


class WriterTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_is_immutable_after_enqueue(self):
        sink = GateStream()
        capture = Capture("unused", stream=sink)
        self.addCleanup(sink.release.set)
        try:
            value = {"tokens": [1, 2]}
            capture.write("snapshot", value=value)
            value["tokens"].append(3)
        finally:
            sink.release.set()
            result = capture.close()
        event = next(row for row in map(json.loads, sink.records) if row["kind"] == "snapshot")
        self.assertEqual(event["value"], {"tokens": [1, 2]})
        self.assertTrue(result["valid_capture"])
        self.assertTrue(all(row["serialize_ns"] >= 0 and row["enqueue_ns"] >= 0 for row in result["callback_timings"]))

    async def test_overflow_is_counted_and_invalidates_capture(self):
        sink = GateStream()
        capture = Capture("unused", stream=sink, queue_capacity=1)
        self.addCleanup(sink.release.set)
        await asyncio.to_thread(sink.entered.wait, 1)
        capture.write("queued")
        capture.write("overflow")
        sink.release.set()
        capture.drain()
        result = capture.close()
        self.assertFalse(result["valid_capture"])
        self.assertEqual(result["dropped_records"], 1)
        self.assertEqual(result["failures"][0]["sequence"], 3)
        summary = json.loads(sink.records[-1])
        self.assertFalse(summary["valid_capture"])
        self.assertEqual(result["pending_records"], 0)

    async def test_failed_write_is_visible_with_no_fake_persisted_count(self):
        sink = GateStream(fail=True)
        sink.release.set()
        capture = Capture("unused", stream=sink)
        result = capture.close()
        self.assertFalse(result["valid_capture"])
        self.assertEqual(result["persisted_records"], 0)
        self.assertEqual(result["failed_writes"], result["queued_records"])
        self.assertFalse(result["summary_written"])

    async def test_close_has_finite_drain_and_preserves_invalid_status(self):
        sink = GateStream()
        capture = Capture("unused", stream=sink, close_timeout=0.02)
        self.addCleanup(sink.release.set)
        await asyncio.to_thread(sink.entered.wait, 1)
        started = time.monotonic()
        result = capture.close()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.2)
        self.assertFalse(result["valid_capture"])
        self.assertGreater(result["pending_records"], 0)
        sink.release.set()
        capture.thread.join(timeout=1)
        summary = json.loads(sink.records[-1])
        self.assertFalse(summary["valid_capture"])
        self.assertTrue(any(item["stage"] == "close_timeout" for item in summary["failures"]))

    async def test_serialization_failure_retains_sequence(self):
        sink = GateStream()
        sink.release.set()
        capture = Capture("unused", stream=sink)
        capture.write("bad", value=float("nan"))
        result = capture.close()
        self.assertFalse(result["valid_capture"])
        self.assertEqual(result["dropped_records"], 1)
        self.assertEqual(result["failures"][0]["stage"], "serialization")


class SlowWriterCancelTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_capture_worker.CaptureTests.asyncSetUp
    asyncTearDown = test_capture_worker.CaptureTests.asyncTearDown
    generate = test_capture_worker.CaptureTests.generate
    consume = test_capture_worker.CaptureTests.consume

    async def test_blocked_disk_does_not_block_loop_or_native_abort(self):
        self.capture.uninstall()
        self.capture.close()
        sink = GateStream()
        self.capture = install_capture(type(self.engine), self.path, stream=sink)
        self.addCleanup(sink.release.set)
        await asyncio.to_thread(sink.entered.wait, 1)
        self.engine.wait = True
        task = asyncio.create_task(self.consume(self.generate()))
        await asyncio.wait_for(self.engine.started.wait(), 0.2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.2)
        self.assertEqual(self.engine.events[-1], ("abort", "client-1-internal", True))
        self.assertEqual(sink.records, [])
        sink.release.set()
        result = self.capture.close()
        self.assertTrue(result["valid_capture"])
        self.assertTrue(any(json.loads(row)["kind"] == "engine_abort_ack" for row in sink.records))
