# Cache reset before each replay

This procedure is based on pinned source. It has not been exercised against a live worker. The helper's CPU tests use a mock runtime.

## Reset endpoint

Both Dynamo main `f5d3353e2167bb0f0d729085eb5bc9183bf4b222` and v1.5.0 `b83b1d9304ebfc624709ac46db32b1b6f1ff1615` register a worker request-plane endpoint named `<namespace>.<component>.clear_kv_blocks`. The handler calls `engine_client.reset_prefix_cache(reset_connector=True)` and returns an error if vLLM reports `False`.

The Python vLLM worker's `register_engine_routes` does not register `/engine/control/clear_kv_blocks`. A test payload for that HTTP path exists elsewhere in Dynamo, so it is easy to mistake it for this worker's API. Use the request-plane endpoint for the planned backend.

Run the helper inside the worker's pinned Python environment, with the same discovery and request/event-plane settings. For the earlier file-discovery setup, after checking the namespace and component:

```bash
DYN_DISCOVERY_BACKEND=file DYN_EVENT_PLANE=zmq \
  python experiments/dynamo-upstream/001-speculative-prefill/gpu-readiness/reset_worker.py \
  --endpoint dynamo.backend.clear_kv_blocks --execute
```

Without `--execute`, it prints a preview. With it, the helper waits at most 30 seconds by default, requires exactly one endpoint instance, targets that instance directly and checks the exact success response. It reports `backend_reset_reported: true` while leaving both router and event confirmation false.

## Backend success does not finish the reset

In pinned vLLM 0.28.0 (`2cf0a6915ce544dc493a0990f2ea38d81601128a`), the block pool refuses reset while blocks remain in use. On success it clears the prefix-cache map and block hashes, then queues `AllBlocksCleared` when KV events are enabled. The scheduler publishes queued events from `update_from_output`; its reset method does not publish them directly.

An idle worker can therefore acknowledge reset before the router sees the event. Use this sequence during live preflight:

1. Stop admission and wait for all real requests and warmups to finish. Start event capture before clearing anything.
2. Call `clear_kv_blocks` and save its response and worker identity.
3. If the clear event has not arrived, send one token-input completion with `prompt: [42]`, `max_tokens: 1` and `temperature: 0`. Confirm the backend received one input token. At most two computed tokens fit below the configured 16-token cache block, so this can advance event publication without creating a full reusable block. Record the canary separately from study requests.
4. Observe `AllBlocksCleared` for the expected worker and epoch, with no event-sequence gap. Dynamo converts it to `KvCacheEventData::Cleared`; the radix-tree handler removes that worker's indexed blocks.
5. Confirm that the frontend index actually applied the clear. Capturing the wire event alone leaves this unverified. Save an index acknowledgement or empty-state observation; if unavailable, use a verified frontend recovery/restart from the cleared publisher state. If empty backend and index state cannot both be demonstrated, reload the deployment or stop the footprint trial.
6. Record the reset evidence. The first real replay request must report zero cached tokens. This check alone does not prove that no unrelated blocks remain resident.

The canary and router confirmation still need a live smoke test. Any change to the deployed vLLM version requires checking its reset/event behavior again.

## Saved source

[reset-source.json](reset-source.json) links and hashes all 15 files in `reset-source.tar.gz`. These cover worker registration and handling, the Python client API, vLLM reset/event publication, and Dynamo event conversion/index removal. The archive contains the exact clean git blobs, independent of the patched working tree.

```bash
uv run python experiments/dynamo-upstream/001-speculative-prefill/gpu-readiness/reset_sources.py
```

To rebuild the source archive from the pinned local checkouts and the existing verified vLLM source archive, add `--capture`. It makes no network or server requests.
