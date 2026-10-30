"""Capture the pinned worker's request boundary without changing its inputs."""
import asyncio
import contextvars
import functools
import hashlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import queue
import runpy
import sys
import threading
import time
import uuid


STATS = ("arrival_time", "queued_ts", "scheduled_ts", "first_token_ts",
         "last_token_ts", "first_token_latency", "num_generation_tokens")


def token_ids(prompt):
    ids = prompt.get("prompt_token_ids") if isinstance(prompt, dict) else getattr(prompt, "prompt_token_ids", None)
    if isinstance(ids, (list, tuple)) and all(type(value) is int for value in ids):
        return list(ids)
    return None


class Capture:
    def __init__(self, destination, *, queue_capacity=4096, close_timeout=2.0, stream=None):
        if queue_capacity < 1:
            raise ValueError("capture queue must have a positive capacity")
        self.stream = stream if stream is not None else Path(destination).open("a", buffering=1)
        self.process_id = uuid.uuid4().hex
        self.sequence = 0
        self.requests = {}
        self.internal_ids = {}
        self.active = contextvars.ContextVar("worker_capture", default=None)
        self.originals = {}
        self.engine_class = None
        self.queue = queue.Queue(maxsize=queue_capacity)
        self.condition = threading.Condition()
        self.stop = threading.Event()
        self.close_timeout = close_timeout
        self.closed = False
        self.summary_written = False
        self.queued = self.persisted = self.failed = self.dropped = 0
        self.failures = []
        self.callback_timings = []
        self.thread = threading.Thread(target=self._writer, name="worker-capture-writer", daemon=True)
        self.thread.start()
        self.write("capture_start", computed_prefill_tokens=None,
                   computed_prefill_status="not_exposed_by_AsyncLLM")

    def write(self, kind, **fields):
        started = time.monotonic_ns()
        self.sequence += 1
        sequence = self.sequence
        record = {"schema_version": 1, "kind": kind, "sequence": self.sequence,
                  "capture_process_id": self.process_id, "pid": os.getpid(),
                  "worker_monotonic_ns": time.monotonic_ns(),
                  "worker_wall_ns": time.time_ns(), **fields}
        try:
            encoded = json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n"
        except Exception as error:
            with self.condition:
                self.dropped += 1
                self.failures.append({"sequence": sequence, "stage": "serialization", "error": repr(error)})
                self.callback_timings.append({"sequence": sequence, "serialize_ns": time.monotonic_ns() - started,
                                              "enqueue_ns": 0})
            return
        serialized = time.monotonic_ns()
        with self.condition:
            try:
                if self.closed:
                    raise RuntimeError("capture already closed")
                self.queue.put_nowait((sequence, encoded))
                self.queued += 1
            except (queue.Full, RuntimeError) as error:
                self.dropped += 1
                self.failures.append({"sequence": sequence, "stage": "enqueue", "error": type(error).__name__})
            self.callback_timings.append({"sequence": sequence, "serialize_ns": serialized - started,
                                          "enqueue_ns": time.monotonic_ns() - serialized})

    def status(self):
        with self.condition:
            pending = self.queued - self.persisted - self.failed
            return {"attempted_records": self.sequence, "queued_records": self.queued,
                    "capture_process_id": self.process_id,
                    "persisted_records": self.persisted, "failed_writes": self.failed,
                    "dropped_records": self.dropped, "pending_records": pending,
                    "failures": list(self.failures), "callback_timings": list(self.callback_timings),
                    "valid_capture": self.closed and pending == 0 and not self.failures,
                    "summary_written": self.summary_written}

    def _writer(self):
        try:
            while not self.stop.is_set() or not self.queue.empty():
                try:
                    sequence, encoded = self.queue.get(timeout=0.02)
                except queue.Empty:
                    continue
                try:
                    self.stream.write(encoded)
                    self.stream.flush()
                except Exception as error:
                    with self.condition:
                        self.failed += 1
                        self.failures.append({"sequence": sequence, "stage": "write", "error": repr(error)})
                else:
                    with self.condition:
                        self.persisted += 1
                finally:
                    self.queue.task_done()
                    with self.condition:
                        self.condition.notify_all()
            summary = {"kind": "writer_summary", "capture_process_id": self.process_id, **self.status()}
            summary.pop("summary_written")
            self.stream.write(json.dumps(summary, separators=(",", ":"), allow_nan=False) + "\n")
            self.stream.flush()
            self.summary_written = True
        except Exception as error:
            with self.condition:
                self.failures.append({"sequence": None, "stage": "writer_summary", "error": repr(error)})
        finally:
            try:
                self.stream.close()
            except Exception as error:
                with self.condition:
                    self.failures.append({"sequence": None, "stage": "stream_close", "error": repr(error)})

    def drain(self, timeout=1.0):
        with self.condition:
            return self.condition.wait_for(lambda: self.queued == self.persisted + self.failed, timeout)

    def close(self):
        if not self.closed:
            self.write("capture_close")
            self.closed = True
            self.stop.set()
        self.thread.join(timeout=self.close_timeout)
        if self.thread.is_alive():
            with self.condition:
                self.failures.append({"sequence": None, "stage": "close_timeout", "error": "writer did not drain before deadline"})
        result = self.status()
        result["valid_capture"] = result["valid_capture"] and result["summary_written"] and not self.thread.is_alive()
        if self.thread.is_alive():
            result["drain_timeout"] = True
        return result

    def uninstall(self):
        for name, original in self.originals.items():
            setattr(self.engine_class, name, original)


def install_capture(engine_class, destination, **writer_options):
    capture = Capture(destination, **writer_options)
    capture.engine_class = engine_class
    originals = {name: getattr(engine_class, name) for name in ("generate", "_add_request", "abort")}
    capture.originals = originals
    signatures = {name: inspect.signature(method) for name, method in originals.items()}

    def bind(name, self, args, kwargs):
        bound = signatures[name].bind(self, *args, **kwargs)
        bound.apply_defaults()
        return bound.arguments

    @functools.wraps(originals["_add_request"])
    async def add_request(self, *args, **kwargs):
        bound = bind("_add_request", self, args, kwargs)
        request = bound["request"]
        state = capture.active.get()
        internal_id = request.request_id
        external_id = getattr(request, "external_req_id", None)
        link = {"capture_id": state["capture_id"] if state else None,
                "external_request_id": external_id, "engine_request_id": internal_id}
        capture.internal_ids[(id(self), internal_id)] = link
        if state is not None:
            state["engine_request_ids"].append(internal_id)
        capture.write("engine_link", **link, processed_prompt_ids=token_ids(request),
                      external_matches_generate=external_id == state["request_id"] if state else None)
        result = await originals["_add_request"](self, *args, **kwargs)
        capture.write("engine_submit_return", **link)
        return result

    @functools.wraps(originals["abort"])
    async def abort(self, *args, **kwargs):
        bound = bind("abort", self, args, kwargs)
        value = bound["request_id"]
        # Do not consume an iterable that the engine still needs to read.
        ids = [value] if isinstance(value, str) else list(value) if isinstance(value, (list, tuple)) else None
        links = []
        for request_id in ids or ():
            if bound["internal"]:
                link = capture.internal_ids.get((id(self), request_id))
                links.append(link or {"engine_request_id": request_id, "capture_id": None})
            else:
                links.extend({"capture_id": state["capture_id"], "external_request_id": request_id,
                              "engine_request_ids": list(state["engine_request_ids"])}
                             for state in capture.requests.values()
                             if state["instance_id"] == id(self) and state["request_id"] == request_id)
        call_id = uuid.uuid4().hex
        fields = {"abort_call_id": call_id, "request_ids": ids,
                  "internal": bound["internal"], "links": links}
        capture.write("engine_abort_call", **fields)
        try:
            result = await originals["abort"](self, *args, **kwargs)
        except BaseException as error:
            capture.write("engine_abort_error", **fields, error_type=type(error).__name__, error=str(error))
            raise
        capture.write("engine_abort_ack", **fields,
                      acknowledgement="AsyncLLM.abort Python await returned", gpu_execution_stopped=None)
        return result

    @functools.wraps(originals["generate"])
    async def generate(self, *args, **kwargs):
        bound = bind("generate", self, args, kwargs)
        params = bound["sampling_params"]
        kind = getattr(getattr(params, "output_kind", None), "name", None)
        state = {"capture_id": uuid.uuid4().hex, "instance_id": id(self),
                 "request_id": bound["request_id"], "engine_request_ids": [],
                 "prompt_ids": token_ids(bound["prompt"]), "output_kind": kind,
                 "cache_salt": bound["prompt"].get("cache_salt") if isinstance(bound["prompt"], dict) else getattr(bound["prompt"], "cache_salt", None),
                 "outputs": {}, "cached_tokens": None, "cache_creation_tokens": None,
                 "engine_stats": {name: None for name in STATS}, "finished": False,
                 "output_count": 0, "computed_prefill_tokens": None,
                 "computed_prefill_status": "not_exposed_by_AsyncLLM"}
        capture.requests[state["capture_id"]] = state
        headers = bound.get("trace_headers") or {}
        capture.write("engine_enter", **state,
                      trace_headers={key: value for key, value in headers.items()
                                     if key.lower() in ("traceparent", "tracestate", "x-request-id", "request-id")},
                      sampling={name: getattr(params, name, None)
                                for name in ("n", "max_tokens", "min_tokens", "ignore_eos", "temperature", "seed")})
        iterator = originals["generate"](self, *args, **kwargs)
        terminal = "stream_ended_without_finished"
        try:
            while True:
                context_token = capture.active.set(state)
                try:
                    output = await anext(iterator)
                except StopAsyncIteration:
                    break
                finally:
                    capture.active.reset(context_token)
                state["output_count"] += 1
                chunks = []
                for choice in getattr(output, "outputs", ()):
                    index = getattr(choice, "index", 0)
                    ids = list(getattr(choice, "token_ids", ()) or ())
                    chunks.append({"index": index, "token_ids": ids,
                                   "finish_reason": getattr(choice, "finish_reason", None)})
                    if kind == "DELTA":
                        state["outputs"].setdefault(index, []).extend(ids)
                    elif kind in ("CUMULATIVE", "FINAL_ONLY"):
                        state["outputs"][index] = ids
                for source, target in (("num_cached_tokens", "cached_tokens"),
                                       ("num_cache_creation_tokens", "cache_creation_tokens")):
                    value = getattr(output, source, None)
                    if value is not None:
                        state[target] = value
                stats = getattr(output, "metrics", None)
                for name in STATS:
                    value = getattr(stats, name, None)
                    if value is not None:
                        state["engine_stats"][name] = value
                prompt = getattr(output, "prompt_token_ids", None)
                if prompt is not None:
                    state["output_prompt_matches_entry"] = list(prompt) == state["prompt_ids"] if state["prompt_ids"] is not None else None
                state["finished"] = bool(getattr(output, "finished", False))
                capture.write("engine_output", capture_id=state["capture_id"], request_id=state["request_id"],
                              output_request_id=getattr(output, "request_id", None),
                              output_id_matches_generate=getattr(output, "request_id", None) == state["request_id"],
                              output_count=state["output_count"], chunks=chunks, output_kind=kind,
                              finished=state["finished"], cached_tokens=state["cached_tokens"],
                              cache_creation_tokens=state["cache_creation_tokens"], engine_stats=state["engine_stats"])
                if state["finished"]:
                    terminal = "finished"
                yield output
        except BaseException as error:
            terminal = "cancelled" if isinstance(error, asyncio.CancelledError) else "consumer_closed" if isinstance(error, GeneratorExit) else "error"
            state["error_type"] = type(error).__name__
            state["error"] = str(error)
            raise
        finally:
            context_token = capture.active.set(state)
            try:
                # Closing the observer must also close the engine generator.
                await iterator.aclose()
            finally:
                capture.write("engine_result", **state, terminal=terminal,
                              output_ids_status="reported" if kind in ("DELTA", "CUMULATIVE", "FINAL_ONLY") else "unknown_output_kind")
                capture.active.reset(context_token)

    engine_class.generate = generate
    engine_class._add_request = add_request
    engine_class.abort = abort
    return capture


def verify_runtime():
    pins = json.loads(Path(__file__).with_name("source-pins.json").read_text())
    distribution = importlib.metadata.distribution("vllm")
    if distribution.version != pins["vllm_version"]:
        raise RuntimeError("Capture requires the pinned vLLM version")
    for relative, expected in pins["files"].items():
        actual = hashlib.sha256(Path(distribution.locate_file(relative)).read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError("Pinned vLLM source mismatch: " + relative)


def write_close_status(destination, status):
    path = Path(str(destination) + ".close.json")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(status, separators=(",", ":"), allow_nan=False) + "\n")
    os.replace(temporary, path)
    return path


def main():
    verify_runtime()
    from vllm.v1.engine.async_llm import AsyncLLM
    capture = install_capture(AsyncLLM, os.environ["REEDCODE_ENGINE_CAPTURE"])
    try:
        sys.argv[0] = "dynamo.vllm"
        runpy.run_module("dynamo.vllm", run_name="__main__")
    finally:
        status = capture.close()
        write_close_status(os.environ["REEDCODE_ENGINE_CAPTURE"], status)
        if not status["valid_capture"]:
            fields = {key: value for key, value in status.items() if key != "callback_timings"}
            print("WORKER_CAPTURE_INVALID " + json.dumps(fields), file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
