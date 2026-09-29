"""Collect vLLM phase times and cache samples around a model call.

Metric definitions: https://docs.vllm.ai/en/latest/usage/metrics/
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import math
import re
import time
from urllib.parse import urlsplit

import httpx


COUNTERS = {
    "prompt_tokens": ("vllm:prompt_tokens_total", "vllm:prompt_tokens"),
    "generation_tokens": ("vllm:generation_tokens_total", "vllm:generation_tokens"),
    "prefix_cache_queries_tokens": ("vllm:prefix_cache_queries_total", "vllm:prefix_cache_queries"),
    "prefix_cache_hits_tokens": ("vllm:prefix_cache_hits_total", "vllm:prefix_cache_hits"),
    "requests_finished": ("vllm:request_success_total", "vllm:request_success"),
}
HISTOGRAMS = {
    "prefill": "vllm:request_prefill_time_seconds",
    "decode": "vllm:request_decode_time_seconds",
    "queue": "vllm:request_queue_time_seconds",
    "time_to_first_token": "vllm:time_to_first_token_seconds",
    "end_to_end": "vllm:e2e_request_latency_seconds",
}
GAUGES = {
    "kv_cache_usage_fraction": ("vllm:kv_cache_usage_perc",),
    "requests_running": ("vllm:num_requests_running",),
    "requests_waiting": ("vllm:num_requests_waiting",),
}
NAMES = {name for aliases in (*COUNTERS.values(), *GAUGES.values()) for name in aliases}
NAMES.update(name + suffix for name in HISTOGRAMS.values() for suffix in ("_count", "_sum", "_created"))
NAMES.update(name.removesuffix("_total") + "_created" for aliases in COUNTERS.values() for name in aliases)
NAMES.add("process_start_time_seconds")
SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+([^\s]+)(?:\s+.*)?$")
LABEL = re.compile(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\[\\"n])*)"\s*(?:,|$)')


def parse_metrics(body: str, model_name: str | None = None) -> dict:
    """Read the numeric Prometheus series used by this collector."""
    metrics: dict = {}
    for line in body.splitlines():
        match = SAMPLE.fullmatch(line.strip())
        if not match or match[1] not in NAMES:
            continue
        labels = {}
        raw = match[2] or ""
        cursor = 0
        while cursor < len(raw):
            label = LABEL.match(raw, cursor)
            if not label or label[1] in labels:
                raise ValueError("Invalid metric labels")
            labels[label[1]] = re.sub(r'\\([\\"n])', lambda m: "\n" if m[1] == "n" else m[1], label[2])
            cursor = label.end()
        if model_name is not None and match[1] != "process_start_time_seconds":
            if labels.get("model_name") != model_name:
                continue
        try:
            value = float(match[3])
        except ValueError:
            continue
        if not math.isfinite(value):
            continue
        key = tuple(sorted(labels.items()))
        series = metrics.setdefault(match[1], {})
        if key in series:
            raise ValueError("Duplicate metric series")
        series[key] = value
    return metrics


def _delta(snapshots: list[dict], aliases: tuple[str, ...]) -> dict:
    name = next((name for name in aliases if name in snapshots[0]), None)
    if name is None or any(name not in sample for sample in snapshots):
        return {"value": None, "status": "missing"}
    series = [sample[name] for sample in snapshots]
    if any(values.keys() != series[0].keys() for values in series):
        return {"value": None, "status": "series_changed"}
    created = name.removesuffix("_total").removesuffix("_sum").removesuffix("_count") + "_created"
    creation_times = [sample.get(created) for sample in snapshots]
    if any(value != creation_times[0] for value in creation_times[1:]):
        return {"value": None, "status": "counter_reset"}
    for before, after in zip(series, series[1:]):
        if any(after[key] < before[key] for key in before):
            return {"value": None, "status": "counter_reset"}
    return {"value": sum(series[-1][key] - value for key, value in series[0].items()), "status": "ok"}


def _gauge(snapshot: dict | None, aliases: tuple[str, ...], maximum: bool = False) -> float | None:
    if snapshot is None:
        return None
    values = next((snapshot[name].values() for name in aliases if snapshot.get(name)), None)
    if values is None:
        return None
    # Engine capacities are unknown, so adding their cache fractions is meaningless.
    return max(values) if maximum else sum(values)


class ServerMetricsWindow:
    """Wrap one awaited inference call; read ``result`` after the context exits.

    Attribution needs a dedicated server idle at both boundaries. Only the
    supplied URL is queried. Reports omit request bodies, keys, URLs, label
    values, and raw exceptions to keep credentials out of saved traces.
    """

    def __init__(self, url: str | None, *, model_name: str | None = None,
                 sample_interval: float = 0.1, timeout: float = 2.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        if not math.isfinite(sample_interval) or not math.isfinite(timeout) or sample_interval <= 0 or timeout <= 0:
            raise ValueError("Sampling interval and timeout must be positive")
        self.url = url
        self.model_name = model_name
        self.sample_interval = sample_interval
        self.timeout = timeout
        self.transport = transport
        self.result: dict = {"status": "disabled" if url is None else "not_started"}
        self._samples: list[dict] = []
        self._errors: list[str] = []
        self._client = None
        self._task = None

    async def _scrape(self, phase: str) -> dict | None:
        try:
            response = await self._client.get(self.url)
            response.raise_for_status()
            metrics = parse_metrics(response.text, self.model_name)
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as error:
            # HTTP exception strings can contain credentials from a URL.
            self._errors.append(type(error).__name__)
            return None
        self._samples.append({"phase": phase, "at": time.perf_counter(), "metrics": metrics})
        return metrics

    async def _sample_loop(self):
        while True:
            await asyncio.sleep(self.sample_interval)
            await self._scrape("during")

    async def __aenter__(self):
        if self.url is None:
            return self
        try:
            parsed = urlsplit(self.url)
        except ValueError:
            self.result = {"status": "unavailable", "errors": ["InvalidMetricsURL"]}
            return self
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            self.result = {"status": "unavailable", "errors": ["InvalidMetricsURL"]}
            return self
        self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=False,
                                        trust_env=False, transport=self.transport)
        try:
            self._before = await self._scrape("before")
        except BaseException:
            await self._client.aclose()
            raise
        self._task = asyncio.create_task(self._sample_loop())
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        if self._client is None:
            return False
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        try:
            self._after = await self._scrape("after")
            self.result = self._report()
        finally:
            await self._client.aclose()
        return False

    def _report(self) -> dict:
        snapshots = [sample["metrics"] for sample in self._samples]
        boundaries_valid = self._before is not None and self._after is not None
        restarted = bool(snapshots) and any(
            sample.get("process_start_time_seconds") != snapshots[0].get("process_start_time_seconds")
            for sample in snapshots[1:]
        )

        def delta(aliases):
            if not boundaries_valid:
                return {"value": None, "status": "scrape_failed"}
            if restarted:
                return {"value": None, "status": "server_restarted"}
            return _delta(snapshots, aliases)

        counters = {key: {
            **delta(names), "unit": "requests" if key == "requests_finished" else "tokens",
            "metric": next((name for name in names if self._before is not None and name in self._before), None),
        } for key, names in COUNTERS.items()}
        histograms = {}
        for key, name in HISTOGRAMS.items():
            count, total = delta((name + "_count",)), delta((name + "_sum",))
            valid = count["status"] == total["status"] == "ok"
            histograms[key] = {
                "metric": name,
                "count": count["value"] if valid else None,
                "sum_seconds": total["value"] if valid else None,
                "mean_seconds": total["value"] / count["value"] if valid and count["value"] > 0 else None,
                "status": "ok" if valid else next(item["status"] for item in (count, total) if item["status"] != "ok"),
            }
        gauges = {}
        during = [sample for sample in self._samples if sample["phase"] == "during"]
        for key, names in GAUGES.items():
            maximum = key == "kv_cache_usage_fraction"
            values = [_gauge(sample["metrics"], names, maximum) for sample in during]
            values = [value for value in values if value is not None]
            gauges[key] = {
                "metric": names[0], "unit": "fraction" if maximum else "requests",
                "before": _gauge(self._before, names, maximum),
                "after": _gauge(self._after, names, maximum),
                "during_sample_count": len(values),
                "during_sampled_max": max(values) if values else None,
            }

        warnings = ["Server-wide metrics require a dedicated idle server; sampling cannot rule out other traffic.",
                    "Server phase times are wall times, not GPU kernel times. Sampled occupancy can miss the peak."]
        busy = any(
            gauges[key][boundary] is not None and gauges[key][boundary] > 0
            for key in ("requests_running", "requests_waiting") for boundary in ("before", "after")
        )
        concurrent = any(
            sum(_gauge(sample["metrics"], GAUGES[key]) or 0 for key in ("requests_running", "requests_waiting")) > 1
            for sample in during
        )
        finished = counters["requests_finished"]["value"]
        if busy or concurrent or (finished is not None and finished > 1):
            warnings.append("Other or unfinished requests were observed; do not attribute these deltas to one call.")
        if finished != 1:
            warnings.append("Exactly one completion was not observed. Metrics may be missing or published after the final scrape.")
        idle_unknown = any(value["before"] is None or value["after"] is None for key, value in gauges.items() if key != "kv_cache_usage_fraction")
        if idle_unknown:
            warnings.append("Idle server boundaries could not be checked because request gauges were missing.")
        available = any(item["value"] is not None for item in counters.values()) or any(
            item["sum_seconds"] is not None for item in histograms.values()
        ) or any(item["before"] is not None or item["during_sample_count"] for item in gauges.values())
        complete = all(item["status"] == "ok" for item in (*counters.values(), *histograms.values()))
        status = "ok" if complete and not self._errors else "partial" if available else "unavailable"
        return {
            "status": status, "scope": "server_window", "sample_interval_seconds": self.sample_interval,
            "successful_scrapes": len(self._samples), "failed_scrapes": len(self._errors),
            "errors": sorted(set(self._errors)), "server_restarted": restarted,
            "counter_deltas": counters, "histograms": histograms, "gauges": gauges,
            "attribution_warning": busy or concurrent or idle_unknown or finished != 1,
            "warnings": warnings,
        }
