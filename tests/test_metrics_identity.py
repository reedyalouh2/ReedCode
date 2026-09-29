import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
import uuid

import httpx

from dynamo_metrics import EpochMetrics
from metrics_identity import IdentityUnavailable, ProcessMonitor, validate_identity
from test_server_metrics import exposition


def identity(challenge):
    return {"schema_version": 1, "challenge": challenge,
            "boot_id": "00000000-0000-4000-8000-000000000001",
            "monitor_id": "00000000-0000-4000-8000-000000000002",
            "backend_pid": 10, "engine_pids": [20], "metrics_port": 8081,
            "metrics_socket_inodes": ["123"],
            "processes": [{"pid": 10, "parent_pid": 1, "start_ticks": 100},
                          {"pid": 20, "parent_pid": 10, "start_ticks": 200}]}


def without_start(step=0, start=100):
    return "\n".join(line for line in exposition(step, start=start).splitlines()
                     if not line.startswith("process_start_time_seconds"))


class IdentityCollectorTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, transform=None, *, include_start=False):
        after = False
        calls = []

        def respond(request):
            calls.append(request.url.path)
            if request.url.path == "/identity":
                body = identity(request.url.params["challenge"])
                if transform:
                    body = transform(body, after, calls)
                if isinstance(body, httpx.Response):
                    return body
                return httpx.Response(200, json=body)
            body = exposition(int(after)) if include_start else without_start(int(after))
            return httpx.Response(200, text=body)

        async with EpochMetrics("http://fixture/metrics", identity_url="http://fixture/identity",
                                sample_interval=0.001, transport=httpx.MockTransport(respond)) as window:
            after = True
        return window.result, calls

    async def test_live_bound_identity_replaces_missing_prometheus_identity(self):
        report, calls = await self.collect()
        self.assertTrue(report["valid_boundaries"])
        self.assertTrue(report["restart_observable"])
        self.assertFalse(report["process_start_observable"])
        self.assertEqual(report["restart_identity_source"], "process_sidecar")
        self.assertEqual(report["counter_deltas"]["requests_finished"]["value"], 1)
        self.assertEqual(len(report["process_identities"]), 1)
        self.assertEqual(calls[:3], ["/identity", "/metrics", "/identity"])
        self.assertEqual(calls.count("/identity"), 2 * calls.count("/metrics"))

    async def test_missing_start_remains_invalid_without_opt_in(self):
        async with EpochMetrics("http://fixture/metrics", sample_interval=0.001,
                                transport=httpx.MockTransport(lambda _: httpx.Response(200, text=without_start()))) as window:
            pass
        self.assertFalse(window.result["valid_boundaries"])
        self.assertFalse(window.result["restart_observable"])

    async def test_engine_restart_is_detected_even_when_counters_increase(self):
        def restart(body, after, calls):
            if after:
                body["processes"][1]["start_ticks"] += 1
            return body
        report, _ = await self.collect(restart)
        self.assertFalse(report["valid_boundaries"])
        self.assertTrue(report["server_restarted"])
        self.assertEqual(report["counter_deltas"]["requests_finished"]["status"], "server_restarted")

    async def test_restart_during_metrics_scrape_is_detected(self):
        def restart(body, after, calls):
            if "/metrics" in calls:
                body["boot_id"] = str(uuid.UUID(int=3))
            return body
        report, _ = await self.collect(restart)
        self.assertFalse(report["valid_boundaries"])
        self.assertTrue(report["server_restarted"])

    async def test_sidecar_restart_and_listener_rebind_invalidate(self):
        for field, value in (("monitor_id", str(uuid.UUID(int=4))), ("metrics_socket_inodes", ["999"])):
            def change(body, after, calls):
                if after:
                    body[field] = value
                return body
            report, _ = await self.collect(change)
            self.assertFalse(report["valid_boundaries"])
            self.assertTrue(report["server_restarted"])

    async def test_unavailable_stale_malformed_identity_fails_even_with_start_metric(self):
        variants = [lambda body: httpx.Response(503), lambda body: {**body, "challenge": "old"},
                    lambda body: {**body, "processes": []}, lambda body: [],
                    lambda body: {**body, "engine_pids": [999]}]
        for variant in variants:
            report, _ = await self.collect(lambda body, after, calls: variant(body), include_start=True)
            self.assertFalse(report["valid_boundaries"])
            self.assertFalse(report["restart_observable"])
            self.assertEqual(report["counter_deltas"]["requests_finished"]["status"], "identity_unavailable")
            self.assertNotIn("http://", json.dumps(report))

    async def test_existing_prometheus_restart_detection_still_applies(self):
        after = False
        def respond(request):
            if request.url.path == "/identity":
                return httpx.Response(200, json=identity(request.url.params["challenge"]))
            return httpx.Response(200, text=exposition(int(after), start=200 if after else 100))
        async with EpochMetrics("http://fixture/metrics", identity_url="http://fixture/identity",
                                sample_interval=0.001, transport=httpx.MockTransport(respond)) as window:
            after = True
        self.assertTrue(window.result["server_restarted"])
        self.assertFalse(window.result["valid_boundaries"])

    async def test_normal_exit_finishes_active_identity_bracket(self):
        for delayed_call in (3, 4):
            seen = asyncio.Event()
            release = asyncio.Event()
            calls = 0
            async def respond(request):
                nonlocal calls
                if request.url.path == "/identity":
                    calls += 1
                    if calls == delayed_call:
                        seen.set()
                        await release.wait()
                    return httpx.Response(200, json=identity(request.url.params["challenge"]))
                return httpx.Response(200, text=without_start())
            window = EpochMetrics("http://fixture/metrics", identity_url="http://fixture/identity",
                                  sample_interval=0.001, transport=httpx.MockTransport(respond))
            await window.__aenter__()
            await asyncio.wait_for(seen.wait(), timeout=1)
            exiting = asyncio.create_task(window.__aexit__(None, None, None))
            await asyncio.sleep(0)
            self.assertFalse(exiting.done())
            release.set()
            await asyncio.wait_for(exiting, timeout=1)
            self.assertTrue(window.result["valid_boundaries"])
            self.assertEqual(window.result["errors"], [])
            self.assertTrue(all(row["before"] and row["after"] for row in window.result["identity_observations"]))

    async def test_stuck_sampler_is_bounded_and_invalidated(self):
        seen = asyncio.Event()
        calls = 0
        async def respond(request):
            nonlocal calls
            if request.url.path == "/identity":
                calls += 1
                if calls == 3:
                    seen.set()
                    await asyncio.Event().wait()
                return httpx.Response(200, json=identity(request.url.params["challenge"]))
            return httpx.Response(200, text=without_start())
        window = EpochMetrics("http://fixture/metrics", identity_url="http://fixture/identity", timeout=0.01,
                              sample_interval=0.001, transport=httpx.MockTransport(respond))
        await window.__aenter__()
        await asyncio.wait_for(seen.wait(), timeout=1)
        await asyncio.wait_for(window.__aexit__(None, None, None), timeout=1)
        self.assertFalse(window.result["valid_boundaries"])
        self.assertIn("SamplerStopTimeout", window.result["errors"])
        self.assertTrue(any(row["before"] is None for row in window.result["identity_observations"]))

    async def test_cancelled_identity_bracket_is_recorded_as_incomplete(self):
        second_identity = asyncio.Event()
        calls = 0
        async def respond(request):
            nonlocal calls
            if request.url.path == "/identity":
                calls += 1
                if calls == 2:
                    second_identity.set()
                    await asyncio.Event().wait()
                return httpx.Response(200, json=identity(request.url.params["challenge"]))
            return httpx.Response(200, text=without_start())
        window = EpochMetrics("http://fixture/metrics", identity_url="http://fixture/identity")
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            window._client = client
            task = asyncio.create_task(window._scrape("during"))
            await asyncio.wait_for(second_identity.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(window._samples), 1)
        self.assertIsNone(window._identity_samples[0]["after"])

    def test_invalid_identity_configuration_rejected(self):
        for url in ("file:///tmp/identity", "http://user:secret@fixture/identity", "http://fixture/?token=x", "http://fixture/#x"):
            with self.assertRaises(ValueError):
                EpochMetrics("http://fixture/metrics", identity_url=url)
        with self.assertRaises(ValueError):
            EpochMetrics(None, identity_url="http://fixture/identity")


class ProcessMonitorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        boot = self.root / "sys/kernel/random/boot_id"
        boot.parent.mkdir(parents=True)
        boot.write_text(str(uuid.UUID(int=1)))
        (self.root / "net").mkdir()
        (self.root / "net/tcp").write_text(
            "header\n0: 0100007F:1F91 00000000:0000 0A 0:0 00:0 0 0 0 123\n")
        self.process(10, 1, 100)
        self.process(20, 10, 200)
        (self.root / "10/fd/3").symlink_to("socket:[123]")

    def process(self, pid, parent, start, state="S"):
        directory = self.root / str(pid)
        directory.mkdir(exist_ok=True)
        (directory / "fd").mkdir(exist_ok=True)
        fields = [state, str(parent)] + ["0"] * 17 + [str(start)]
        (directory / "stat").write_text(f"{pid} (name with ) parentheses) " + " ".join(fields))

    def monitor(self):
        return ProcessMonitor(10, [20], 8081, proc_root=self.root)

    def test_identity_reads_real_fields_and_validates(self):
        monitor = self.monitor()
        snapshot = monitor.snapshot()
        self.assertEqual(snapshot["processes"][1]["start_ticks"], 200)
        fingerprint = validate_identity({**snapshot, "challenge": "x"}, "x")
        self.assertEqual(len(fingerprint), 64)
        reversed_body = copy.deepcopy(snapshot)
        reversed_body["processes"].reverse()
        self.assertEqual(fingerprint, validate_identity({**reversed_body, "challenge": "y"}, "y"))

    def test_pid_reuse_engine_exit_and_new_children_fail_closed(self):
        monitor = self.monitor()
        self.process(20, 10, 300)
        with self.assertRaises(IdentityUnavailable):
            monitor.snapshot()
        self.process(20, 10, 200, "Z")
        with self.assertRaises(IdentityUnavailable):
            monitor.snapshot()
        self.process(20, 10, 200)
        self.process(30, 20, 400)
        with self.assertRaises(IdentityUnavailable):
            monitor.snapshot()

    def test_kernel_restart_listener_rebind_and_foreign_listener_fail(self):
        monitor = self.monitor()
        boot = self.root / "sys/kernel/random/boot_id"
        boot.write_text(str(uuid.UUID(int=2)))
        with self.assertRaises(IdentityUnavailable):
            monitor.snapshot()
        boot.write_text(str(uuid.UUID(int=1)))
        tcp = self.root / "net/tcp"
        tcp.write_text(tcp.read_text().replace(" 123", " 999"))
        with self.assertRaises(IdentityUnavailable):
            monitor.snapshot()
        (self.root / "10/fd/4").symlink_to("socket:[999]")
        with self.assertRaises(IdentityUnavailable):
            monitor.snapshot()

    def test_engine_must_be_explicit_and_related_to_backend(self):
        with self.assertRaises(ValueError):
            ProcessMonitor(10, [], 8081, proc_root=self.root)
        self.process(20, 1, 200)
        with self.assertRaises(IdentityUnavailable):
            self.monitor()


if __name__ == "__main__":
    unittest.main()
