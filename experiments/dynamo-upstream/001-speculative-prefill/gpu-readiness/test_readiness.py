import asyncio
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import time

from prepare import ROOT
from replay import selected, send, usage_counts
from replay import run as run_replay
from reset_sources import verify as verify_reset_sources
from reset_worker import call_worker
from verify import read_gzip, verify
from kv_report import classify, collector, identity_module, resolve_blocks, report, computed_prefill
from prepare import sha
from trial import check_frontend_proof, latest_clear


class ReadinessTests(unittest.TestCase):
    def test_every_frozen_payload_matches_its_native_builder(self):
        result = verify()
        self.assertEqual(result["cases"], 6)
        self.assertEqual(result["fixed_exact_prefixes"], 6)

    def test_conditions_select_one_warmup_branch_and_preserve_real_inputs(self):
        for name, real_count in (("short", 5), ("long", 3)):
            plan = read_gzip(ROOT / f"fixtures/{name}-replay.json.gz")
            off = selected(plan, "off")
            self.assertEqual(len(off), real_count)
            for condition in ("stock", "fixed"):
                rows = selected(plan, condition)
                self.assertEqual(len(rows), 2 * real_count - 1)
                self.assertEqual([row for row in rows if row["kind"] == "real"], off)
                self.assertTrue(all(row.get("condition", condition) == condition for row in rows))

    def test_rotation_preserves_all_eighteen_trials(self):
        schedule = json.loads((ROOT / "fixtures/schedule.json").read_text())
        self.assertEqual(len(schedule), 18)
        self.assertEqual(len({(r["repeat"], r["session"], r["condition"]) for r in schedule}), 18)
        for repeat, expected in ((1, ["off", "stock", "fixed"]), (2, ["stock", "fixed", "off"]), (3, ["fixed", "off", "stock"])):
            for session in ("short", "long"):
                self.assertEqual([r["condition"] for r in schedule if r["repeat"] == repeat and r["session"] == session], expected)

    def test_unknown_cached_usage_is_not_zero(self):
        for usage in (None, {}, {"prompt_tokens": 10}, {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": -1}},
                      {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 11}}):
            with self.assertRaises(ValueError):
                usage_counts(usage, 10)
        self.assertEqual(usage_counts({"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 0}}, 10), 0)

    def test_tampered_native_artifact_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "fixtures"
            shutil.copytree(ROOT / "fixtures", target)
            path = target / "fixed-native.json.gz"
            path.write_bytes(path.read_bytes() + b"changed")
            with self.assertRaisesRegex(ValueError, "Frozen file changed"):
                verify(target)

    def test_reported_counts_must_match_saved_token_arrays(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "fixtures"
            shutil.copytree(ROOT / "fixtures", target)
            path = target / "manifest.json"
            manifest = json.loads(path.read_text())
            manifest["checks"][0]["original_tokens"] += 1
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "Reported native checks differ"):
                verify(target)

    def test_sse_capture_keeps_raw_usage_and_request_identity(self):
        payload = {"prompt": [42, 43], "max_tokens": 1, "stream": True}
        frames = [
            {"id": "server-1", "choices": [{"text": "x", "finish_reason": None}]},
            {"id": "server-1", "choices": [{"text": "", "finish_reason": "length"}]},
            {"id": "server-1", "choices": [], "usage": {"prompt_tokens": 2,
                "prompt_tokens_details": {"cached_tokens": 0}}},
        ]
        raw = b"".join(b"data: " + json.dumps(frame).encode() + b"\n\n" for frame in frames) + b"data: [DONE]\n\n"
        with tempfile.TemporaryDirectory() as tmp, patch("replay.urlopen", return_value=io.BytesIO(raw)) as opened:
            output = Path(tmp)
            result, _ = send("http://127.0.0.1:1/v1/completions", {"payload": payload}, "test-1", output, 10)
            self.assertEqual(json.loads(opened.call_args.args[0].data), payload)
            self.assertEqual(opened.call_args.args[0].get_header("X-request-id"), "test-1")
            self.assertEqual(result["response_id"], "server-1")
            self.assertEqual(result["cached_tokens"], 0)
            self.assertIsNotNone(result["ttft_seconds"])
            self.assertEqual((output / "test-1.sse").read_bytes(), raw)

    def test_incomplete_sse_is_saved_and_rejected(self):
        raw = b'data: {"choices": [{"text": "x", "finish_reason": null}]}\n\n'
        with tempfile.TemporaryDirectory() as tmp, patch("replay.urlopen", return_value=io.BytesIO(raw)):
            output = Path(tmp)
            with self.assertRaisesRegex(ValueError, "did not finish normally"):
                send("http://127.0.0.1:1/v1/completions", {"payload": {"prompt": [42]}}, "partial", output, 10)
            self.assertEqual((output / "partial.sse").read_bytes(), raw)

    def test_reset_source_hashes(self):
        self.assertEqual(verify_reset_sources()["source_files_verified"], 15)

    def test_observed_restart_keeps_the_completed_request_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            reset = directory / "reset.json"
            reset.write_text(json.dumps({"backend_cache_empty": True, "router_index_empty": True,
                                        "token_capture_ready": True, "kv_event_capture_ready": True, "process_epoch": "test"}))
            row = {"id": "short/normal-0", "kind": "real", "payload": {"prompt": [42], "max_tokens": 1}}
            measured = {"request_id": "test", "response_id": "response", "prompt_tokens": 1, "cached_tokens": 0,
                        "completion_seconds": .01, "started_unix": time.time(), "ended_unix": time.time()}
            args = SimpleNamespace(directory=directory, session="short", condition="off", repeat=1, execute=True,
                reset_record=reset, output=directory / "run", base_url="http://127.0.0.1:1/v1", max_seconds=10,
                metrics_url="http://127.0.0.1:2/metrics", identity_url="http://127.0.0.1:3/identity")
            with patch("replay.verify", return_value={}), patch("replay.read_gzip", return_value={"sequence": [row]}), \
                 patch("replay.send", return_value=(measured, time.monotonic())), \
                 patch("replay.observe", side_effect=[{"identity_fingerprint": "before"}, {"identity_fingerprint": "after"}]):
                with self.assertRaisesRegex(ValueError, "identity changed"):
                    run_replay(args)
            self.assertEqual(json.loads((args.output / "run.json").read_text())["status"], "failed")
            kept = json.loads((args.output / "requests.jsonl").read_text())
            self.assertEqual(kept["response_id"], "response")
            self.assertIn("identity changed", kept["observations"]["error"])


class ResetTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, instances, responses):
        async def stream():
            for response in responses:
                yield response
        client = SimpleNamespace(wait_for_instances=AsyncMock(return_value=instances),
                                 direct=AsyncMock(return_value=stream()))
        endpoint = SimpleNamespace(client=AsyncMock(return_value=client))
        runtime = SimpleNamespace(endpoint=Mock(return_value=endpoint))
        return runtime, client

    async def test_success_keeps_router_and_event_confirmation_pending(self):
        runtime, client = self.runtime([17], [{"status": "success", "message": "KV cache cleared"}])
        result = await call_worker(runtime, "dynamo.backend.clear_kv_blocks")
        client.direct.assert_awaited_once_with({}, 17, annotated=False)
        self.assertTrue(result["backend_reset_reported"])
        self.assertFalse(result["router_reset_verified"])
        self.assertFalse(result["kv_clear_event_verified"])

    async def test_ambiguous_worker_selection_sends_nothing(self):
        runtime, client = self.runtime([17, 18], [])
        with self.assertRaisesRegex(ValueError, "exactly one"):
            await call_worker(runtime, "dynamo.backend.clear_kv_blocks")
        client.direct.assert_not_awaited()

    async def test_failed_reset_is_rejected(self):
        runtime, _ = self.runtime([17], [{"status": "error", "message": "KV cache reset failed"}])
        with self.assertRaisesRegex(ValueError, "successful reset"):
            await call_worker(runtime, "dynamo.backend.clear_kv_blocks")

    async def test_stalled_discovery_can_be_cancelled(self):
        runtime, _ = self.runtime([17], [])
        async def wait():
            await asyncio.Event().wait()
        runtime.endpoint.return_value.client.side_effect = wait
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(call_worker(runtime, "dynamo.backend.clear_kv_blocks"), .01)


class KVReportTests(unittest.TestCase):
    def test_local_compute_preserves_live_dynamo_labels_and_rejects_ambiguous_or_changed_series(self):
        labels = ('dynamo_component="backend",dynamo_endpoint="generate",dynamo_namespace="dynamo",'
                  'engine="0",model="Qwen/Qwen3-8B",model_name="Qwen/Qwen3-8B",worker_id="worker-a"')
        def metrics(computed, finished):
            return (f'vllm:prompt_tokens_by_source_total{{{labels},source="local_compute"}} {computed}\n'
                    f'vllm:prompt_tokens_by_source_created{{{labels},source="local_compute"}} 123\n'
                    f'vllm:request_success_total{{{labels},finished_reason="length"}} {finished}\n'
                    f'vllm:num_requests_running{{{labels}}} 0\n'
                    f'vllm:num_requests_waiting{{{labels}}} 0\n').encode()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            before_raw, after_raw = metrics(100, 2), metrics(140, 3)
            def measured(raw):
                observations = {}
                for name, body in (('before', before_raw), ('after', raw)):
                    path = directory / (name + '.metrics.txt')
                    path.write_bytes(body)
                    observations[name] = {'identity_fingerprint': 'same-process', 'files': {path.name: sha(body)}}
                return computed_prefill({'observations': observations}, directory)
            result = measured(after_raw)
            self.assertEqual(result['value'], 40)
            self.assertEqual(result['labels']['worker_id'], 'worker-a')
            self.assertEqual(result['labels']['dynamo_endpoint'], 'generate')
            counter = after_raw.splitlines(keepends=True)[0]
            for raw, status in (
                (after_raw + counter.replace(b'worker-a', b'worker-b'), 'local_compute_counter_ambiguous'),
                (after_raw.replace(counter, counter.replace(b'worker-a', b'worker-b')), 'series_changed'),
                (after_raw.replace(b'source="local_compute"', b'source="local_cache_hit"'), 'local_compute_counter_missing'),
                (after_raw.replace(b'} 140\n', b'} 90\n'), 'counter_reset'),
                (after_raw.replace(b'} 123\n', b'} 124\n'), 'counter_reset')):
                with self.subTest(status=status):
                    result = measured(raw)
                    self.assertIsNone(result['value'])
                    self.assertEqual(result['status'], status)

    def test_local_compute_delta_requires_one_idle_unchanged_worker_request(self):
        labels = 'model_name="Qwen/Qwen3-8B",engine="0"'
        def metrics(computed, finished, running=0):
            return (f'vllm:prompt_tokens_by_source_total{{{labels},source="local_compute"}} {computed}\n'
                    f'vllm:prompt_tokens_by_source_created{{{labels},source="local_compute"}} 123\n'
                    f'vllm:request_success_total{{{labels},finished_reason="length"}} {finished}\n'
                    f'vllm:num_requests_running{{{labels}}} {running}\n'
                    f'vllm:num_requests_waiting{{{labels}}} 0\n').encode()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            def snapshot(name, raw, identity="worker"):
                (directory / name).write_bytes(raw)
                return {"identity_fingerprint": identity, "files": {name: sha(raw)}}
            before = snapshot("before.metrics.txt", metrics(100, 2))
            after = snapshot("after.metrics.txt", metrics(140, 3))
            request = {"observations": {"before": before, "after": after}}
            self.assertEqual(computed_prefill(request, directory)["value"], 40)
            after.update(snapshot("after.metrics.txt", metrics(140, 4)))
            self.assertIsNone(computed_prefill(request, directory)["value"])
            after.update(snapshot("after.metrics.txt", metrics(140, 3, 1)))
            self.assertIsNone(computed_prefill(request, directory)["value"])
            after.update(snapshot("after.metrics.txt", metrics(90, 3)))
            self.assertEqual(computed_prefill(request, directory)["status"], "counter_reset")
            after.update(snapshot("after.metrics.txt", metrics(140, 3), "restarted"))
            self.assertEqual(computed_prefill(request, directory)["status"], "identity_changed_or_unknown")
            after.update(snapshot("after.metrics.txt", metrics(140, 3).replace(b'source="local_compute"', b'source="local_cache_hit"')))
            self.assertEqual(computed_prefill(request, directory)["status"], "local_compute_counter_missing")

    def stored(self, hashes, tokens, parent=None):
        return {"type": "BlockStored", "block_hashes": hashes, "parent_block_hash": parent,
                "token_ids": tokens, "block_size": 16, "medium": "GPU"}

    def ledger(self):
        result = collector.PublishedBlocks()
        result.accept(0, "clear", [0.0, [{"type": "AllBlocksCleared"}], 0])
        return result

    def test_parent_dependent_partition_and_context_denominator(self):
        identity = identity_module.BlockIdentity()
        real = identity.blocks(list(range(48)))
        warm = identity.blocks(list(range(16)) + list(range(90, 106)) + list(range(32, 48)))
        ledger = self.ledger()
        ledger.accept(1, "real", [1.0, [self.stored([1, 2, 3], list(range(48)))], 0])
        ledger.accept(2, "warm", [2.0, [self.stored([4, 5], list(range(90, 106)) + list(range(32, 48)), 1)], 0])
        report = classify(ledger, identity, real, warm, real, 48)
        self.assertEqual(report["counts"], {"shared_real_and_warm": 1, "real_only": 2,
                                          "warmup_only": 2, "other_known": 0, "unresolved_ancestry": 0})
        self.assertEqual(report["warmup_only_to_current_full_blocks"], 2 / 3)
        self.assertEqual(report["warmup_only_to_current_tokens"], 2 / 3)
        ledger.accept(3, "remove", [3.0, [{"type": "BlockRemoved", "block_hashes": [1], "medium": "GPU"}], 0])
        self.assertTrue(classify(ledger, identity, real, warm, real, 48)["attributable"])

    def test_missing_ancestor_and_sequence_gap_suppress_attribution(self):
        identity = identity_module.BlockIdentity()
        real = identity.blocks(list(range(16)))
        ledger = self.ledger()
        ledger.accept(1, "missing", [1.0, [self.stored([2], list(range(16)), 99)], 0])
        report = classify(ledger, identity, real, set(), real, 16)
        self.assertFalse(report["attributable"])
        self.assertIsNone(report["warmup_only_to_current_tokens"])
        self.assertEqual(report["counts"]["unresolved_ancestry"], 1)
        ledger = self.ledger()
        ledger.accept(2, "gap", [2.0, [self.stored([1], list(range(16)))], 0])
        self.assertFalse(classify(ledger, identity, real, set(), real, 16)["attributable"])

    def test_long_reverse_order_ancestry_does_not_use_python_recursion(self):
        history = {str(i): {"parent": str(i - 1) if i else None, "token_ids": [i] * 16} for i in reversed(range(2000))}
        resolved, missing = resolve_blocks(history, identity_module.BlockIdentity())
        self.assertEqual(len(resolved), 2000)
        self.assertFalse(missing)

    def test_new_clear_requires_empty_set_and_matching_topic(self):
        rows = [{"sequence": 0, "payload_sha256": "clear", "topic": "kv-events", "batch": [10.0, [{"type": "AllBlocksCleared"}], 0]}]
        with patch("trial.events", return_value=rows):
            self.assertEqual(latest_clear(Path("unused"), 9, "kv-events")["sequence"], 0)
            self.assertIsNone(latest_clear(Path("unused"), 11, "kv-events"))
            with self.assertRaises(ValueError):
                latest_clear(Path("unused"), 9, "wrong-topic")
        rows.append({"sequence": 1, "payload_sha256": "stored", "topic": "kv-events", "batch": [11.0, [self.stored([1], list(range(16)))], 0]})
        with patch("trial.events", return_value=rows):
            self.assertIsNone(latest_clear(Path("unused"), 9, "kv-events"))

    def test_frontend_claim_requires_distinct_epoch_and_matching_proof_file(self):
        from prepare import sha
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace"
            path.write_text("CPU fixture for proof validation")
            proof = {"router_index_empty": True, "frontend_pid": 12, "frontend_epoch": "new",
                     "previous_frontend_epoch": "old", "started_unix": 10,
                     "proof_file": str(path), "proof_sha256": sha(path.read_bytes())}
            self.assertEqual(check_frontend_proof(proof), path)
            proof["frontend_epoch"] = "old"
            with self.assertRaisesRegex(ValueError, "did not change"):
                check_frontend_proof(proof)
            proof["frontend_epoch"] = "new"
            path.write_text("changed")
            with self.assertRaisesRegex(ValueError, "changed"):
                check_frontend_proof(proof)

    def test_complete_off_trial_report_and_closing_gap_failure(self):
        from prepare import json_bytes, sha
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "replay").mkdir()
            rows = selected(read_gzip(ROOT / "fixtures/short-replay.json.gz"), "off")
            requests, wire = [], []
            identity = identity_module.BlockIdentity()
            batches = [{"sequence": 0, "payload_sha256": "start", "topic": "kv-events", "batch": [99.0, [{"type": "AllBlocksCleared"}], 0]}]
            for i, row in enumerate(rows):
                ids = row["payload"]["prompt"]
                requests.append({"id": row["id"], "request_id": f"http-{i}", "response_id": f"public-{i}",
                    "started_unix": 100 + i * 2, "ended_unix": 100.5 + i * 2, "prompt_tokens": len(ids),
                    "cached_tokens": 0, "ttft_seconds": .1, "completion_seconds": .5})
                wire.append({"kind": "model", "request_id": f"internal-{i}", "input_token_ids": ids,
                    "input_tokens_sha256": sha(json_bytes(ids)), "complete": True,
                    "trace_headers": {"x-frontend-send-ts-ns": str(int((100.1 + i * 2) * 1e9))}})
                identity.blocks(ids)
                parent, hashes = 0, []
                for start in range(0, len(ids) - 15, 16):
                    parent = identity.nodes[(parent, tuple(ids[start:start + 16]))]
                    hashes.append(parent)
                batches.append({"sequence": i + 1, "payload_sha256": str(i), "topic": "kv-events",
                    "batch": [100.25 + i * 2, [self.stored(hashes, ids[:16 * len(hashes)])], 0]})
            close = len(batches)
            batches.append({"sequence": close, "payload_sha256": "end", "topic": "kv-events", "batch": [120.0, [{"type": "AllBlocksCleared"}], 0]})
            (folder / "kv-frames.jsonl").write_text("CPU-only test fixture; event decoder is mocked here\n")
            (folder / "kv-config.json").write_text(json.dumps({"hostname": "test", "topic": "kv-events"}))
            (folder / "trial.json").write_text(json.dumps({"status": "replay_completed", "clear_event": {"sequence": 0},
                                                         "closing_clear": {"sequence": close, "payload_sha256": "end"}}))
            (folder / "wire.json").write_text(json.dumps({"requests": wire}))
            raw = "".join(json.dumps(r) + "\n" for r in requests).encode()
            (folder / "replay/requests.jsonl").write_bytes(raw)
            (folder / "replay/run.json").write_text(json.dumps({"status": "http_replay_completed", "session": "short",
                "condition": "off", "repeat": 1, "hostname": "test", "files": {"requests.jsonl": sha(raw)}}))
            with patch("kv_report.events", return_value=batches):
                result = report(folder, folder / "wire.json")
            self.assertTrue(result["attributable"])
            self.assertEqual(result["terminal"]["counts"]["warmup_only"], 0)
            self.assertEqual(len(result["input_linkage"]), 5)
            self.assertFalse(result["physical_kv_bytes_measured"])
            self.assertIsNone(result["scheduled_prefill_tokens"])
            unexpected = [dict(row) for row in batches]
            unexpected[1] = {**unexpected[1], "batch": [11.0, [{"type": "AllBlocksCleared"}], 0]}
            with patch("kv_report.events", return_value=unexpected):
                with self.assertRaisesRegex(ValueError, "Unexpected cache clear"):
                    report(folder, folder / "wire.json")
            with patch("kv_report.events", return_value=batches[:-2] + batches[-1:]):
                broken = report(folder, folder / "wire.json")
            self.assertFalse(broken["attributable"])
            self.assertIsNone(broken["terminal"]["warmup_only_to_current_tokens"])


class LiveHintSmokeTests(unittest.TestCase):
    def generated(self):
        return {"finish_reason": "tool_calls", "message": {"role": "assistant", "content": "",
            "tool_calls": [{"id": "call-1", "type": "function", "function": {
                "name": "read_file", "arguments": '{"path":"project.txt"}'}}]}}

    def test_only_the_actual_validated_tool_message_enters_the_followup(self):
        from live_hint_smoke import initial_request, following_request, FILE_TEXT
        request, response = initial_request(), self.generated()
        following = following_request(request, response)
        self.assertTrue(request["nvext"]["agent_hints"]["speculative_prefill"])
        self.assertNotIn("nvext", following)
        self.assertEqual(following["tools"], request["tools"])
        self.assertEqual(following["messages"][-2], response["message"])
        self.assertEqual(following["messages"][-1]["content"], FILE_TEXT)
        response["message"]["tool_calls"][0]["function"]["arguments"] = '{"path":"/etc/passwd"}'
        with self.assertRaisesRegex(ValueError, "differs from the fixed"):
            following_request(request, response)

    def test_stream_capture_preserves_entity_bytes_and_requires_done(self):
        import httpx
        from live_hint_smoke import initial_request, send
        response = self.generated()
        frames = [{"id": "chat-1", "choices": [{"index": 0, "delta": {
            "tool_calls": [{"index": 0, **response["message"]["tool_calls"][0]}]}, "finish_reason": "tool_calls"}]},
            {"id": "chat-1", "choices": [], "usage": {"prompt_tokens": 50,
                "prompt_tokens_details": {"cached_tokens": 0}}}]
        body = b"".join(b"data: " + json.dumps(frame).encode() + b"\n\n" for frame in frames)
        captured = []
        def respond(request):
            captured.append(request)
            return httpx.Response(200, stream=httpx.ByteStream(body + b"data: [DONE]\n\n"))
        with tempfile.TemporaryDirectory() as tmp, httpx.Client(transport=httpx.MockTransport(respond)) as client:
            output = Path(tmp)
            parsed, meta = send(client, "http://127.0.0.1:8000/v1/chat/completions", initial_request(),
                                "observer-1", "session-1", output, 0, 10)
            self.assertEqual(parsed["message"]["tool_calls"], response["message"]["tool_calls"])
            self.assertEqual(captured[0].headers["x-request-id"], "observer-1")
            self.assertEqual(captured[0].content, (output / "request-0.json").read_bytes())
            self.assertEqual((output / "response-0.sse").read_bytes(), body + b"data: [DONE]\n\n")
            self.assertEqual(meta["status"], 200)
            with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=httpx.ByteStream(body)))) as broken:
                with self.assertRaisesRegex(ValueError, "final marker"):
                    send(broken, "http://127.0.0.1:8000/v1/chat/completions", initial_request(),
                         "observer-2", "session-1", output, 1, 10)
            self.assertTrue((output / "http-1.json").exists())

    def test_wire_report_uses_captured_ids_and_rejects_a_divergent_fixed_warmup(self):
        from contextlib import redirect_stdout
        from live_hint_smoke import check_wire
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            manifest = {"status": "http_smoke_completed", "variant": "fixed", "files": {},
                        "requests": [{"request_id": "first"}, {"request_id": "second"}]}
            (output / "smoke.json").write_text(json.dumps(manifest))
            def row(name, stamp, ids, max_tokens=256, observer=None):
                return {"request_id": name, "kind": "model", "complete": True,
                    "trace_headers": {"x-frontend-send-ts-ns": str(stamp)}, "input_token_ids": ids,
                    "request": {"stop_conditions": {"max_tokens": max_tokens}},
                    "http_link": {"http_request_id": observer} if observer else {}}
            wire = {"requests": [row("runtime-1", 1, list(range(40)), observer="first"),
                row("internal-warmup", 2, list(range(48)), 1),
                row("runtime-2", 3, list(range(60)), observer="second")]}
            path = output / "wire.json"
            path.write_text(json.dumps(wire))
            with redirect_stdout(io.StringIO()):
                result = check_wire(output, path)
            self.assertEqual(result["matched_full_block_tokens"], 48)
            self.assertTrue(result["warmup_is_exact_prefix"])
            wire["requests"][1]["input_token_ids"][16] = 999
            path.write_text(json.dumps(wire))
            with self.assertRaisesRegex(ValueError, "Fixed internal warmup diverges"):
                check_wire(output, path)
            saved = json.loads((output / "warmup-wire-check.json").read_text())
            self.assertEqual(saved["first_divergence_index"], 16)
            self.assertFalse(saved["warmup_is_exact_prefix"])


class PhaseControlTests(unittest.TestCase):
    def test_identity_rebind_only_allows_backend_adoption_by_init(self):
        import copy
        from phase_control import validate_repin
        original = {'boot_id': 'boot', 'backend_pid': 10, 'engine_pids': [12],
                    'metrics_port': 8081, 'metrics_socket_inodes': ['123'],
                    'processes': [{'pid': 10, 'parent_pid': 9, 'start_ticks': 100},
                                  {'pid': 12, 'parent_pid': 10, 'start_ticks': 120}]}
        adopted = copy.deepcopy(original)
        adopted['processes'][0]['parent_pid'] = 1
        validate_repin(original, adopted)
        validate_repin(adopted, adopted)
        cases = []
        for field, value in [('start_ticks', 101), ('parent_pid', 99)]:
            changed = copy.deepcopy(adopted); changed['processes'][0][field] = value; cases.append(changed)
        changed = copy.deepcopy(adopted); changed['processes'][1]['parent_pid'] = 1; cases.append(changed)
        changed = copy.deepcopy(adopted); changed['processes'].pop(); cases.append(changed)
        for key, value in [('metrics_socket_inodes', ['999']), ('boot_id', 'reboot'), ('engine_pids', [13])]:
            changed = copy.deepcopy(adopted); changed[key] = value; cases.append(changed)
        for changed in cases:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                validate_repin(original, changed)

    def setup_phase(self, folder):
        import phase_control
        directory = folder / "prefill/epochs/test"
        directory.mkdir(parents=True)
        active = {"phase": "prefill", "epoch": "test", "directory": str(directory),
            "status": "ready_for_live_gates", "processes_file": str(directory / "processes.json"),
            "frontend_state": str(folder / "prefill/frontend-state.json"), "kv_directory": str(directory / "kv")}
        phase_control.save(folder / "phase-active.json", active)
        args = SimpleNamespace(study_dir=folder, control_python=Path("/observer/python"),
                               backend_python="/backend/python", stop_at_unix=time.time() + 1000)
        return args, active, directory

    def test_failed_smoke_stops_owned_phase_and_retains_failure_before_trials(self):
        import phase_control
        with tempfile.TemporaryDirectory() as tmp:
            args, active, directory = self.setup_phase(Path(tmp))
            with patch("phase_control.command", side_effect=RuntimeError("smoke failed")) as command, \
                 patch("phase_control.repin_identity"), \
                 patch("phase_control.stop", return_value={"status": "stopped"}) as stopped:
                with self.assertRaisesRegex(RuntimeError, "smoke failed"):
                    phase_control.prefill(args)
            stopped.assert_called_once_with(args)
            self.assertEqual(command.call_count, 1)
            self.assertFalse((directory / "capture-proof.json").exists())
            report = json.loads((directory / "prefill-batch.json").read_text())
            self.assertEqual(report["status"], "failed")
            self.assertFalse(report["completed_trials"])

    def test_missing_kv_evidence_never_creates_a_capture_ready_claim(self):
        import phase_control
        with tempfile.TemporaryDirectory() as tmp:
            args, active, directory = self.setup_phase(Path(tmp))
            with patch("phase_control.command") as command, \
                 patch("phase_control.repin_identity"), \
                 patch("phase_control.wire_snapshot", return_value=directory / "wire.json"), \
                 patch("phase_control.kv_capture_proof", side_effect=ValueError("KV gap")), \
                 patch("phase_control.stop", return_value={"status": "stopped"}) as stopped:
                with self.assertRaisesRegex(ValueError, "KV gap"):
                    phase_control.prefill(args)
            stopped.assert_called_once_with(args)
            self.assertFalse((directory / "capture-proof.json").exists())
            self.assertFalse(any(str(ROOT / "trial.py") in call.args[1] for call in command.call_args_list))

    def test_stop_uses_current_frontend_state_after_trial_restarts(self):
        import phase_control
        with tempfile.TemporaryDirectory() as tmp:
            args, active, directory = self.setup_phase(Path(tmp))
            phase_control.save(Path(active["processes_file"]), {"frontend": {"pid": 10, "start_ticks": 100},
                                                                "backend": {"pid": 20, "start_ticks": 200}})
            phase_control.save(Path(active["frontend_state"]), {"frontend_pid": 30, "frontend_start_ticks": 300,
                                                                 "frontend_epoch": "latest"})
            with patch("phase_control.hook.start_ticks", return_value=None), \
                 patch("phase_control.hook.terminate_owned") as terminated:
                result = phase_control.stop(args)
            self.assertEqual(result["status"], "stopped")
            self.assertIn(((30, 300), {}), [(call.args, call.kwargs) for call in terminated.call_args_list])
            self.assertNotIn(10, [call.args[0] for call in terminated.call_args_list])



if __name__ == "__main__":
    unittest.main()
