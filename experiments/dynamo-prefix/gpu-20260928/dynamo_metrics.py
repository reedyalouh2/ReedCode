"""Measure a whole replay epoch, including background preparation."""

import asyncio
import math
import time
from urllib.parse import urlsplit
import uuid

import httpx

from metrics_identity import validate_identity

from server_metrics import COUNTERS, GAUGES, HISTOGRAMS, ServerMetricsWindow, _delta, _gauge


class EpochMetrics(ServerMetricsWindow):
    def __init__(self, *args, drain_timeout=10.0, identity_url=None, **kwargs):
        super().__init__(*args, **kwargs)
        if not math.isfinite(drain_timeout) or drain_timeout <= 0:
            raise ValueError("drain_timeout must be positive and finite")
        self.drain_timeout = drain_timeout
        self._drained = False
        if identity_url is not None:
            parsed = urlsplit(identity_url)
            if (self.url is None or parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValueError("Identity URL requires metrics and a plain HTTP(S) endpoint")
        self.identity_url = identity_url
        self._identity_samples = []
        self._identities = {}
        self._stop_sampling = asyncio.Event()

    async def _identity(self):
        challenge = uuid.uuid4().hex
        try:
            response = await self._client.get(self.identity_url, params={"challenge": challenge},
                                              headers={"Cache-Control": "no-cache"})
            response.raise_for_status()
            body = response.json()
            fingerprint = validate_identity(body, challenge)
            self._identities[fingerprint] = {key: body[key] for key in (
                "schema_version", "boot_id", "monitor_id", "backend_pid", "engine_pids",
                "processes", "metrics_port", "metrics_socket_inodes")}
            return fingerprint
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as error:
            self._errors.append("Identity" + type(error).__name__)
            return None

    async def _scrape(self, phase):
        if self.identity_url is None:
            return await super()._scrape(phase)
        # Bracketing also catches a restart while Prometheus serves its response.
        before = after = None
        try:
            before = await self._identity()
            metrics = await super()._scrape(phase)
            after = await self._identity()
            return metrics
        finally:
            self._identity_samples.append({"phase": phase, "before": before, "after": after})

    async def _sample_loop(self):
        while not self._stop_sampling.is_set():
            try:
                await asyncio.wait_for(self._stop_sampling.wait(), timeout=self.sample_interval)
            except TimeoutError:
                if not self._stop_sampling.is_set():
                    await self._scrape("during")

    async def __aexit__(self, exc_type, exc, traceback):
        if self._client is None:
            return False
        try:
            self._stop_sampling.set()
            # Finish an active identity bracket before starting the drain.
            try:
                await asyncio.wait_for(self._task, timeout=3 * self.timeout)
            except TimeoutError:
                self._errors.append("SamplerStopTimeout")
            deadline = time.perf_counter() + self.drain_timeout
            quiet = 0
            previous = None
            while True:
                self._after = await self._scrape("drain")
                sample = self._after
                idle = sample is not None and all(
                    _gauge(sample, GAUGES[key]) == 0
                    for key in ("requests_running", "requests_waiting"))
                counters = {name: sample.get(name) for aliases in COUNTERS.values() for name in aliases} if sample else None
                quiet = quiet + 1 if idle and counters == previous else 0
                previous = counters
                if quiet >= 3:
                    self._drained = True
                    break
                if time.perf_counter() >= deadline:
                    break
                await asyncio.sleep(self.sample_interval)
            self.result = self._report()
        finally:
            await self._client.aclose()
        return False

    def _report(self):
        snapshots = [s["metrics"] for s in self._samples]
        boundaries = self._before is not None and self._after is not None
        process_restarted = any(s.get("process_start_time_seconds") != snapshots[0].get("process_start_time_seconds")
                                for s in snapshots[1:]) if snapshots else False
        identity_values = [sample[key] for sample in self._identity_samples for key in ("before", "after")]
        identity_observable = bool(identity_values) and all(identity_values)
        identity_restarted = len({value for value in identity_values if value is not None}) > 1
        restarted = process_restarted or identity_restarted

        def delta(names):
            if not boundaries:
                return {"value": None, "status": "scrape_failed"}
            if restarted:
                return {"value": None, "status": "server_restarted"}
            if self.identity_url is not None and not identity_observable:
                return {"value": None, "status": "identity_unavailable"}
            return _delta(snapshots, names)

        counters = {key: delta(names) for key, names in COUNTERS.items()}
        phases = {key: {"seconds": delta((name + "_sum",)),
                        "requests": delta((name + "_count",))}
                  for key, name in HISTOGRAMS.items()}
        samples = [(s["at"], _gauge(s["metrics"], GAUGES["kv_cache_usage_fraction"], True))
                   for s in self._samples]
        coverage = bool(samples) and all(v is not None and 0 <= v <= 1 for _, v in samples)
        area = sum((t2 - t1) * (a + b) / 2 for (t1, a), (t2, b) in zip(samples, samples[1:])) if coverage else None
        idle_before = self._before is not None and all(
            _gauge(self._before, GAUGES[key]) == 0 for key in ("requests_running", "requests_waiting"))
        process_observable = bool(snapshots) and all(s.get("process_start_time_seconds") for s in snapshots)
        restart_observable = identity_observable if self.identity_url is not None else process_observable
        valid = boundaries and idle_before and self._drained and restart_observable and not restarted and not self._errors
        return {
            "scope": "epoch", "valid_boundaries": bool(valid),
            "idle_before": idle_before, "drained": self._drained,
            "server_restarted": restarted, "errors": self._errors,
            "restart_observable": bool(restart_observable),
            "restart_identity_source": "process_sidecar" if self.identity_url is not None else "prometheus",
            "process_start_observable": bool(process_observable),
            "identity_observations": self._identity_samples,
            "process_identities": self._identities,
            "sample_interval_seconds": self.sample_interval,
            "counter_deltas": counters, "phase_totals": phases,
            "sampled_kv_max_fraction": max(v for _, v in samples) if coverage else None,
            "sampled_kv_fraction_seconds": area,
            "samples": [{"offset_s": t - samples[0][0], "kv_max_fraction": v} for t, v in samples],
            "notes": ["All traffic on this metrics endpoint is included.",
                      "The cache fraction is the largest engine fraction; capacities are unknown.",
                      "Three quiet intervals cannot exclude later work or unrelated clients.",
                      "A sidecar must watch every engine behind this exact metrics endpoint; tunnel binding is operator supplied."],
        }
