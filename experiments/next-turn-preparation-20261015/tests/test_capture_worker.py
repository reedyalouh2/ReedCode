import asyncio
from enum import Enum
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest

from capture_worker import install_capture


class OutputKind(Enum):
    CUMULATIVE = 0
    DELTA = 1
    FINAL_ONLY = 2


def output(request_id, ids, *, finished=False, cached=None, metrics=None):
    return NS(request_id=request_id, outputs=[NS(index=0, token_ids=ids, finish_reason="length" if finished else None)],
              prompt_token_ids=[11, 12, 13], num_cached_tokens=cached,
              num_cache_creation_tokens=None, metrics=metrics, finished=finished)


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        class FakeAsyncLLM:
            def __init__(self):
                self.events = []
                self.outputs = []
                self.started = asyncio.Event()
                self.wait = False
                self.error = None
                self.abort_error = None

            async def _add_request(self, request, prompt, parent_req, index, queue):
                self.events.append(("submit", request.request_id))

            async def generate(self, prompt, sampling_params, request_id, *, trace_headers=None):
                internal = request_id + "-internal"
                await self._add_request(NS(request_id=internal, external_req_id=request_id,
                                           prompt_token_ids=prompt["prompt_token_ids"]), None, None, 0, None)
                self.started.set()
                try:
                    for item in self.outputs:
                        yield item
                    if self.wait:
                        await asyncio.Event().wait()
                    if self.error:
                        raise self.error
                except (asyncio.CancelledError, GeneratorExit):
                    await self.abort(internal, internal=True)
                    raise

            async def abort(self, request_id, internal=False):
                self.events.append(("abort", request_id, internal))
                await asyncio.sleep(0)
                if self.abort_error:
                    raise self.abort_error

        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "capture.jsonl"
        self.capture = install_capture(FakeAsyncLLM, self.path)
        self.engine = FakeAsyncLLM()

    async def asyncTearDown(self):
        self.capture.uninstall()
        self.capture.close()
        self.folder.cleanup()

    def records(self, kind):
        self.assertTrue(self.capture.drain())
        return [item for item in map(json.loads, self.path.read_text().splitlines()) if item["kind"] == kind]

    def generate(self, kind=OutputKind.DELTA, request_id="client-1"):
        return self.engine.generate({"prompt_token_ids": [11, 12, 13]},
                                    NS(output_kind=kind, max_tokens=1), request_id,
                                    trace_headers={"traceparent": "00-abc-def-01", "authorization": "must-not-be-captured"})

    async def consume(self, generator):
        return [item async for item in generator]

    async def test_completed_delta_keeps_link_input_and_native_stats(self):
        stats = NS(arrival_time=100.0, first_token_ts=2.0, first_token_latency=0.1,
                   num_generation_tokens=1)
        self.engine.outputs = [output("client-1", [21], cached=0, metrics=stats),
                               output("client-1", [22], finished=True)]
        returned = await self.consume(self.generate())
        self.assertEqual(returned, self.engine.outputs)
        entered = self.records("engine_enter")[0]
        self.assertEqual(entered["prompt_ids"], [11, 12, 13])
        self.assertEqual(entered["trace_headers"], {"traceparent": "00-abc-def-01"})
        link = self.records("engine_link")[0]
        self.assertEqual(link["capture_id"], entered["capture_id"])
        self.assertEqual(link["engine_request_id"], "client-1-internal")
        self.assertTrue(link["external_matches_generate"])
        result = self.records("engine_result")[0]
        self.assertEqual(result["outputs"], {"0": [21, 22]})
        self.assertEqual(result["cached_tokens"], 0)
        self.assertEqual(result["engine_stats"]["first_token_ts"], 2.0)
        self.assertEqual(result["terminal"], "finished")
        self.assertIsNone(result["computed_prefill_tokens"])
        self.assertTrue(result["output_prompt_matches_entry"])

    async def test_cumulative_outputs_are_not_double_counted(self):
        self.engine.outputs = [output("client-1", [21]), output("client-1", [21, 22], finished=True)]
        await self.consume(self.generate(OutputKind.CUMULATIVE))
        self.assertEqual(self.records("engine_result")[0]["outputs"], {"0": [21, 22]})

    async def test_cancel_before_output_preserves_entry_and_unknown_work(self):
        self.engine.wait = True
        task = asyncio.create_task(self.consume(self.generate()))
        await self.engine.started.wait()
        self.assertEqual(len(self.records("engine_enter")), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        result = self.records("engine_result")[0]
        self.assertEqual(result["terminal"], "cancelled")
        self.assertEqual(result["output_count"], 0)
        self.assertIsNone(result["cached_tokens"])
        self.assertIsNone(result["computed_prefill_tokens"])
        ack = self.records("engine_abort_ack")[0]
        self.assertEqual(ack["links"][0]["capture_id"], result["capture_id"])
        self.assertIsNone(ack["gpu_execution_stopped"])
        self.assertEqual(self.engine.events, [("submit", "client-1-internal"), ("abort", "client-1-internal", True)])

    async def test_cancel_after_output_retains_partial_stats(self):
        self.engine.outputs = [output("client-1", [21], cached=2, metrics=NS(num_generation_tokens=1))]
        self.engine.wait = True
        task = asyncio.create_task(self.consume(self.generate()))
        await self.engine.started.wait()
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        result = self.records("engine_result")[0]
        self.assertEqual(result["outputs"], {"0": [21]})
        self.assertEqual(result["cached_tokens"], 2)
        self.assertEqual(result["engine_stats"]["num_generation_tokens"], 1)
        self.assertFalse(result["finished"])

    async def test_consumer_close_propagates_once_to_engine(self):
        self.engine.outputs = [output("client-1", [21])]
        generator = self.generate()
        await anext(generator)
        await generator.aclose()
        self.assertEqual(len(self.records("engine_abort_ack")), 1)
        self.assertEqual(self.records("engine_result")[0]["terminal"], "consumer_closed")

    async def test_error_keeps_partial_output_and_original_exception(self):
        self.engine.outputs = [output("client-1", [21], cached=2)]
        failure = RuntimeError("engine failed")
        self.engine.error = failure
        with self.assertRaises(RuntimeError) as caught:
            await self.consume(self.generate())
        self.assertIs(caught.exception, failure)
        result = self.records("engine_result")[0]
        self.assertEqual(result["terminal"], "error")
        self.assertEqual(result["cached_tokens"], 2)
        self.assertEqual(result["outputs"], {"0": [21]})

    async def test_external_abort_links_to_internal_id(self):
        self.engine.outputs = [output("client-1", [21], finished=True)]
        await self.consume(self.generate())
        await self.engine.abort("client-1")
        ack = self.records("engine_abort_ack")[0]
        self.assertFalse(ack["internal"])
        self.assertEqual(ack["links"][0]["engine_request_ids"], ["client-1-internal"])

    async def test_abort_error_has_no_success_ack(self):
        self.engine.abort_error = RuntimeError("abort failed")
        with self.assertRaisesRegex(RuntimeError, "abort failed"):
            await self.engine.abort("unknown", internal=True)
        self.assertEqual(len(self.records("engine_abort_error")), 1)
        self.assertEqual(self.records("engine_abort_ack"), [])

    async def test_concurrent_requests_have_distinct_links(self):
        self.engine.wait = True
        tasks = [asyncio.create_task(self.consume(self.generate(request_id=key))) for key in ("one", "two")]
        await asyncio.sleep(0)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        links = self.records("engine_link")
        self.assertEqual({item["external_request_id"] for item in links}, {"one", "two"})
        self.assertEqual(len({item["capture_id"] for item in links}), 2)
        for ack in self.records("engine_abort_ack"):
            self.assertEqual(ack["request_ids"][0], ack["links"][0]["engine_request_id"])

    async def test_unknown_output_kind_keeps_chunks_without_guessing(self):
        self.engine.outputs = [output("client-1", [21], finished=True)]
        await self.consume(self.generate(None))
        self.assertEqual(self.records("engine_output")[0]["chunks"][0]["token_ids"], [21])
        result = self.records("engine_result")[0]
        self.assertEqual(result["outputs"], {})
        self.assertEqual(result["output_ids_status"], "unknown_output_kind")


if __name__ == "__main__":
    unittest.main()
