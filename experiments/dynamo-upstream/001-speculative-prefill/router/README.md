# What the router knows about a warmup

The source does **not** support the claim that a malformed warmup makes the router count incompatible blocks as hits for the intended next prompt. The router indexes the tokens that reached the backend. It then matches later requests against that token prefix. A different rendered suffix ends the match.

The warmup can still consume compute, occupy cache blocks and change which useful blocks survive under pressure. Those effects need a workload measurement. The existing capture does not contain a KV-event stream or an index snapshot that proves which individual warmup blocks remained resident.

This is a source audit of Dynamo release `b83b1d9304ebfc624709ac46db32b1b6f1ff1615` (v1.5.0), main `f5d3353e2167bb0f0d729085eb5bc9183bf4b222`, and the recorded backend's vLLM v0.28.0 source at `2cf0a6915ce544dc493a0990f2ea38d81601128a`. No new server or GPU run was made. [The manifest](manifest.json) records the source URLs and hashes; the raw files are in [source-evidence.tar.gz](source-evidence.tar.gz).

## From warmup to index entry

1. **The stock path makes a normal backend request.** It renders its own message list, supplies those token IDs with `max_tokens=1`, and drains the generated stream. It does this in [the release](https://github.com/ai-dynamo/dynamo/blob/b83b1d9304ebfc624709ac46db32b1b6f1ff1615/lib/llm/src/preprocessor/speculative_prefill.rs#L150-L188) and [main](https://github.com/ai-dynamo/dynamo/blob/f5d3353e2167bb0f0d729085eb5bc9183bf4b222/lib/llm/src/preprocessor/speculative_prefill.rs#L367-L399). Draining lets the request guard complete its accounting and cleanup. It does not pin the cache forever.
2. **vLLM caches full blocks and reports their actual contents.** Its [block pool](https://github.com/vllm-project/vllm/blob/2cf0a6915ce544dc493a0990f2ea38d81601128a/vllm/v1/core/block_pool.py#L230-L373) inserts block hashes and emits `BlockStored` with the request's token IDs, parent block hash, and cache group. The [scheduler](https://github.com/vllm-project/vllm/blob/2cf0a6915ce544dc493a0990f2ea38d81601128a/vllm/v1/core/sched/scheduler.py#L2083-L2098) publishes the collected events. These are backend cache reports; this audit did not observe their delivery for each archived warmup.
3. **Dynamo converts those reports into its index format.** The [vLLM integration](https://github.com/ai-dynamo/dynamo/blob/f5d3353e2167bb0f0d729085eb5bc9183bf4b222/components/src/dynamo/vllm/main.py#L446-L530) connects the publisher when prefix caching and KV events are enabled. The [event conversion](https://github.com/ai-dynamo/dynamo/blob/f5d3353e2167bb0f0d729085eb5bc9183bf4b222/lib/kv-router/src/zmq_wire/convert.rs#L68-L176) preserves the backend parent and block identities; [local token hashes](https://github.com/ai-dynamo/dynamo/blob/f5d3353e2167bb0f0d729085eb5bc9183bf4b222/lib/kv-router/src/zmq_wire/convert.rs#L389-L413) come from the reported tokens. Nothing substitutes the intended future conversation.
4. **A later request must match from the beginning.** The [single-threaded radix tree](https://github.com/ai-dynamo/dynamo/blob/f5d3353e2167bb0f0d729085eb5bc9183bf4b222/lib/kv-router/src/indexer/radix_tree.rs#L150-L278) walks matching block hashes from the root and stops at a different edge. The [concurrent tree](https://github.com/ai-dynamo/dynamo/blob/f5d3353e2167bb0f0d729085eb5bc9183bf4b222/lib/kv-router/src/indexer/concurrent_radix_tree_compressed/matches.rs#L22-L146) follows the same prefix rule. The backend's own [block hash](https://github.com/vllm-project/vllm/blob/2cf0a6915ce544dc493a0990f2ea38d81601128a/vllm/v1/core/kv_cache_utils.py#L577-L604) includes the parent hash, current tokens, and extra keys. Identical text later in a different history is insufficient.

For these checks, the warmup and next request share two full blocks in the tool case and three in the text case. Blocks beyond the divergence cannot add overlap credit for that followup. A different future request could still reuse a matching warmup prefix.

## Events, predictions and eviction

The archived launch enabled prefix caching, backend KV events and KV routing. Its frontend log confirms a direct-ZMQ event connection. The default [router event setting](https://github.com/ai-dynamo/dynamo/blob/f5d3353e2167bb0f0d729085eb5bc9183bf4b222/components/src/dynamo/common/configuration/groups/kv_router_args.py#L406-L418) is enabled.

The frontend [starts its subscriber with the selected indexer](https://github.com/ai-dynamo/dynamo/blob/f5d3353e2167bb0f0d729085eb5bc9183bf4b222/lib/llm/src/kv_router/indexer/ingress.rs#L64-L98). Its recovery target [passes admitted events to `try_apply_event`](https://github.com/ai-dynamo/dynamo/blob/f5d3353e2167bb0f0d729085eb5bc9183bf4b222/lib/llm/src/kv_router/indexer/recovery/target.rs#L67-L76). The tree's [event dispatch](https://github.com/ai-dynamo/dynamo/blob/f5d3353e2167bb0f0d729085eb5bc9183bf4b222/lib/kv-router/src/indexer/radix_tree.rs#L282-L302) selects store, remove or clear for that worker.

In event mode, the [primary index](https://github.com/ai-dynamo/dynamo/blob/f5d3353e2167bb0f0d729085eb5bc9183bf4b222/lib/llm/src/kv_router/indexer/mod.rs#L194-L261) has `primary_records_routing_decisions=false`. A separate optional TTL side index can predict cache state before backend confirmation. Pure approximate mode also records routing decisions. Those predictions still hash [the actual routed tokens](https://github.com/ai-dynamo/dynamo/blob/f5d3353e2167bb0f0d729085eb5bc9183bf4b222/lib/llm/src/kv_router/routing_host/kv.rs#L430-L476). Neither predictive mode was requested in the archived launch.

After request completion, vLLM [frees references while retaining cached hashes](https://github.com/vllm-project/vllm/blob/2cf0a6915ce544dc493a0990f2ea38d81601128a/vllm/v1/core/block_pool.py#L717-L742). These blocks become eviction candidates. Reallocation [evicts cached hashes and emits removal events](https://github.com/vllm-project/vllm/blob/2cf0a6915ce544dc493a0990f2ea38d81601128a/vllm/v1/core/block_pool.py#L658-L700); Dynamo applies `Removed` and `Cleared` to worker ownership. Event-mode entries follow those updates rather than a per-block expiry timer. Delivery and mutation are asynchronous, so an index can lag the backend. That is separate from giving credit to an incompatible token sequence.

## The warmup may itself hit the cache

vLLM [looks up the longest cached prefix](https://github.com/vllm-project/vllm/blob/2cf0a6915ce544dc493a0990f2ea38d81601128a/vllm/v1/core/kv_cache_manager.py#L232-L267) before doing new work. Even a full hit must leave work to obtain logits; this implementation caps the hit at `prompt_length - 1` and uses block alignment.

The saved CPU outputs give this comparison with the original request:

| Case | Warmup input tokens | Shared complete-block tokens with original input | Remaining warmup input after that prefix |
| --- | ---: | ---: | ---: |
| Text | 64 | 48 | 16 |
| Tool | 72 | 32 | 40 |

The last column applies if those shared blocks remain on the selected worker. It is token accounting, not a measurement of FLOPs or latency. The observed extra backend request does not mean that every warmup input token was recomputed.

## What this establishes

| Question | Finding |
| --- | --- |
| Can completed warmups create ordinary cache entries? | Source-confirmed for cacheable full blocks when caching/events are enabled. |
| Does the index treat the malformed suffix as the intended next prompt? | Refuted for the inspected prefix-matching path. Only the shared prefix earns overlap credit. |
| Did every particular warmup block enter the index and survive until followup? | Unmeasured. The archive contains routing fingerprints, not an event-by-event residency record. |
| Did the warmup evict useful blocks or hurt another agent? | Unmeasured. The capture used one worker and did not test cache pressure. |
| Could event lag or approximate predictions temporarily overestimate residency? | Yes by design; this audit does not measure their frequency. |

Before making a shared-server impact claim, record `Stored`/`Removed` events and index lookups alongside the request tokens, then compare hint off, stock and fixed under a matched concurrent workload. Measure useful-block survival and unrelated-request latency. The [GPU check plan](../gpu-check.md) keeps this conditional on measured extra work and approval.
