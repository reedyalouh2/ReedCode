import asyncio
import json
import unittest

import httpx

from server_metrics import HISTOGRAMS, ServerMetricsWindow, parse_metrics


def exposition(step=0, *, running=0, waiting=0, kv=0.0, start=100):
    labels = '{model_name="test-model",engine="0"}'
    lines = [f"process_start_time_seconds {start}"]
    for metric, initial, amount in (
        ("prompt_tokens_total", 100, 40),
        ("generation_tokens_total", 20, 10),
        ("prefix_cache_queries_total", 100, 40),
        ("prefix_cache_hits_total", 50, 16),
        ("request_success_total", 1, 1),
    ):
        lines.append(f"vllm:{metric}{labels} {initial + step * amount}")
    for name in HISTOGRAMS.values():
        lines += [f"{name}_count{labels} {1 + step}", f"{name}_sum{labels} {2 + step * 0.25}"]
    lines += [f"vllm:num_requests_running{labels} {running}",
              f"vllm:num_requests_waiting{labels} {waiting}",
              f"vllm:kv_cache_usage_perc{labels} {kv}"]
    return "\n".join(lines)


class ParseTests(unittest.TestCase):
    def test_filters_model_and_ignores_buckets_unrelated_metrics(self):
        body = '\n'.join([
            '# HELP ignored',
            'vllm:prompt_tokens_total{model_name="other"} 55',
            'vllm:prompt_tokens_total{engine="0",model_name="test-model"} 1.2e3 1234',
            'vllm:request_prefill_time_seconds_bucket{le="1.0"} 200',
            'unrelated{secret="never saved"} 4',
            'vllm:generation_tokens_total{model_name="test-model"} NaN',
        ])
        values = parse_metrics(body, "test-model")
        self.assertEqual(set(values), {"vllm:prompt_tokens_total"})
        self.assertEqual(list(values["vllm:prompt_tokens_total"].values()), [1200])

    def test_escaped_labels_and_duplicates(self):
        values = parse_metrics(r'vllm:prompt_tokens_total{model_name="a\"b\\c\nd",engine="0"} 9')
        labels = dict(next(iter(values["vllm:prompt_tokens_total"])))
        self.assertEqual(labels["model_name"], 'a"b\\c\nd')
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            parse_metrics("vllm:prompt_tokens_total 1\nvllm:prompt_tokens_total 2")


class WindowTests(unittest.IsolatedAsyncioTestCase):
    def test_invalid_sampling_configuration(self):
        for value in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                ServerMetricsWindow(None, sample_interval=value)
            with self.assertRaises(ValueError):
                ServerMetricsWindow(None, timeout=value)

    async def window(self, before, after):
        replies = iter((before, after))
        transport = httpx.MockTransport(lambda request: httpx.Response(200, text=next(replies)))
        async with ServerMetricsWindow("http://server/metrics", sample_interval=100,
                                       transport=transport) as window:
            pass
        return window.result

    async def test_counter_and_histogram_deltas_keep_units_and_sources(self):
        report = await self.window(exposition(), exposition(1))
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["counter_deltas"]["prompt_tokens"], {
            "status": "ok", "value": 40, "unit": "tokens", "metric": "vllm:prompt_tokens_total",
        })
        self.assertEqual(report["histograms"]["prefill"]["count"], 1)
        self.assertEqual(report["histograms"]["prefill"]["sum_seconds"], 0.25)
        self.assertEqual(report["histograms"]["prefill"]["mean_seconds"], 0.25)
        self.assertFalse(report["attribution_warning"])
        self.assertIsNone(report["gauges"]["kv_cache_usage_fraction"]["during_sampled_max"])
        self.assertEqual(report["gauges"]["kv_cache_usage_fraction"]["during_sample_count"], 0)

    async def test_zero_is_distinct_from_missing_and_empty_histogram_mean(self):
        report = await self.window(exposition(), exposition())
        self.assertEqual(report["counter_deltas"]["prompt_tokens"]["value"], 0)
        self.assertEqual(report["histograms"]["prefill"]["count"], 0)
        self.assertIsNone(report["histograms"]["prefill"]["mean_seconds"])
        missing = await self.window("# empty", "# empty")
        self.assertEqual(missing["status"], "unavailable")
        self.assertIsNone(missing["counter_deltas"]["prompt_tokens"]["value"])

    async def test_resets_and_series_changes_do_not_become_negative_savings(self):
        reset = await self.window(exposition(1), exposition())
        self.assertEqual(reset["counter_deltas"]["prompt_tokens"]["status"], "counter_reset")
        self.assertIsNone(reset["counter_deltas"]["prompt_tokens"]["value"])
        changed = await self.window(exposition(), exposition(1).replace('engine="0"', 'engine="1"'))
        self.assertEqual(changed["histograms"]["prefill"]["status"], "series_changed")
        restarted = await self.window(exposition(), exposition(1, start=200))
        self.assertTrue(restarted["server_restarted"])
        self.assertEqual(restarted["counter_deltas"]["prompt_tokens"]["status"], "server_restarted")

    async def test_created_timestamp_detects_reset_even_when_new_counter_is_higher(self):
        report = await self.window(
            exposition() + '\nvllm:prompt_tokens_created{model_name="test-model",engine="0"} 100',
            exposition(1) + '\nvllm:prompt_tokens_created{model_name="test-model",engine="0"} 200',
        )
        self.assertEqual(report["counter_deltas"]["prompt_tokens"]["status"], "counter_reset")

    async def test_inflight_gauges_are_sampled_and_multiple_requests_flagged(self):
        seen = asyncio.Event()
        phase = "before"

        def handle(request):
            if phase == "during":
                seen.set()
                body = exposition(running=2, waiting=1, kv=0.6)
            else:
                body = exposition(1 if phase == "after" else 0)
            return httpx.Response(200, text=body)

        async with ServerMetricsWindow("http://server/metrics", sample_interval=0.001,
                                       transport=httpx.MockTransport(handle)) as window:
            phase = "during"
            await asyncio.wait_for(seen.wait(), timeout=1)
            phase = "after"
        report = window.result
        self.assertEqual(report["gauges"]["kv_cache_usage_fraction"]["during_sampled_max"], 0.6)
        self.assertGreaterEqual(report["gauges"]["kv_cache_usage_fraction"]["during_sample_count"], 1)
        self.assertTrue(report["attribution_warning"])

    async def test_engine_cache_fractions_are_maximum_not_sum(self):
        extra = '\nvllm:kv_cache_usage_perc{model_name="test-model",engine="1"} 0.8'
        report = await self.window(exposition(kv=0.6) + extra, exposition(1, kv=0.7) + extra)
        self.assertEqual(report["gauges"]["kv_cache_usage_fraction"]["before"], 0.8)

    async def test_disabled_and_invalid_urls_do_not_request(self):
        def unexpected(request):
            self.fail("No request should be sent")
        for url in (None, "file:///private/secret", "http://user:password@server/metrics", "http://["):
            async with ServerMetricsWindow(url, transport=httpx.MockTransport(unexpected)) as window:
                pass
            self.assertEqual(window.result["status"], "disabled" if url is None else "unavailable")

    async def test_scrape_error_does_not_hide_inference_error_or_log_url(self):
        def unavailable(request):
            raise httpx.ConnectError("secret URL and credential", request=request)
        with self.assertRaisesRegex(RuntimeError, "inference failed"):
            async with ServerMetricsWindow("http://server/metrics?key=secret", transport=httpx.MockTransport(unavailable)) as window:
                raise RuntimeError("inference failed")
        report = window.result
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["errors"], ["ConnectError"])
        self.assertNotIn("secret", json.dumps(report))

    async def test_missing_request_gauges_prevent_clean_attribution(self):
        def drop_gauges(body):
            return '\n'.join(line for line in body.splitlines() if 'num_requests' not in line)
        report = await self.window(drop_gauges(exposition()), drop_gauges(exposition(1)))
        self.assertTrue(report["attribution_warning"])


if __name__ == "__main__":
    unittest.main()
