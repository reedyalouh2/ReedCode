# Which problem is worth pursuing?

**Paused.** The active work is the [upstream findings queue](dynamo-upstream.md). This document preserves the earlier research proposal.

The target is many coding agents sharing a server. I want to improve completed work per GPU hour while keeping task quality and other agents' latency intact.

The short `noisy-bugfix` trajectories are a regression workload. I have dropped the proposed contention study on those trajectories. The next evidence should come from longer sessions with actual reasoning, user follow-ups, and recorded context changes.

## What we know

The [boundary audit](../experiments/dynamo-prefix/boundary-detail.md) identifies the same four-token empty thinking wrapper at all 42 observed mismatches. No earlier input changes in that pilot.

The [CPU probe](../experiments/dynamo-prefix/user-boundary-results.json) then constructs four-user-turn sessions with thinking enabled. With one synthetic reasoning word followed by 512 diagnostic lines, the first user boundary puts 6,592 prior complete-block tokens beyond the matching prefix. The following prompt has 6,622 tokens beyond its shared complete blocks. Increasing the removed reasoning to 2,048 words leaves that following suffix unchanged. A small edit near the beginning can therefore invalidate a long remaining suffix.

Those inputs are authored fixtures. No model generated their reasoning, no tools ran, and no task was graded. Removed reasoning contributes to the old block count but is absent from the next request. Counting it as necessary recomputation would exaggerate the opportunity.

Template behavior also differs by model. The [pinned family inspection](../experiments/dynamo-prefix/family-template-evidence.json) found:

| Template | Observed rule |
| --- | --- |
| Qwen3-8B | Keeps current tool-loop reasoning; removes it after a new ordinary user message |
| NVIDIA Nemotron-3-Nano-30B-A3B-BF16 | Similar default boundary; already offers `truncate_history_thinking=False` |
| DeepSeek-R1 | Ordinary assistant text strips inline reasoning even before another user; tool-call branches retain it in the inspected cases |

The other families were rendered locally with their own tokenizers. Their serving paths still need verification. Nemotron's hybrid state also requires engine-specific cache accounting.

## What is already known

[Qwen documents the historical-reasoning policy](https://qwen.readthedocs.io/en/v3.0/getting_started/concepts.html). A [vLLM user reproduction](https://discuss.vllm.ai/t/decode-kv-writeback-can-multi-turn-chat-preserve-cache-reuse-with-default-templates/2955) already describes both the empty-wrapper mismatch and reasoning removal, including their effect on decode-cache writeback. Keeping historical reasoning changes model input and memory use. A template-preservation option alone is a weak novelty claim.

[TokenPilot](https://arxiv.org/abs/2606.17016) already studies prompt mutations, cache continuity, and lifecycle-aware context management. [Dynamo's session-prefix proposal](https://github.com/ai-dynamo/dynamo/issues/13279) supplies lineage and ownership information for session-aware cache policies. A contribution here should build on those interfaces.

[KVFlow](https://arxiv.org/html/2507.07400v1) already prioritizes varying suffixes for eviction, protects shared prefixes, and CPU-caches fixed prompt KV. The narrower question is whether an exact, changing reuse boundary can improve admission before decode-cache writeback. [vLLM's Mooncake writeback work](https://github.com/vllm-project/vllm/pull/53129) makes timing critical: a hint arriving after a block was copied cannot save that transfer. I did not find this exact renderer-derived admission rule in these sources; that does not establish novelty.

The [Copilot study](https://arxiv.org/html/2608.00101v1) reports strong within-turn reuse and degradation at user boundaries. Its idle-time results point to eviction, and its telemetry excludes prompt contents. It cannot identify template rewriting as the cause of that degradation.

## A narrower candidate

**Keep the model's normal prompt policy, and tell the serving system when a session can no longer reuse part of its cached history.**

For example, a completed tool loop may leave reasoning and file contents in cache. If the next normal user turn strips that reasoning, blocks after the first changed token cannot serve that continuation. Persisting or transferring those blocks for this session may waste memory and bandwidth. The renderer knows the serialization rule; the harness knows whether the next event is another tool result, a user follow-up, or a committed context replacement.

The proposed signal would identify a session revision, rendering configuration, continuation policy, and exact reusable prefix. Dynamo would use that information before deciding what to retain or transfer. Shared blocks remain live for other owners and in-flight readers. Unknown continuations fall back to ordinary behavior. A revised intent needs acknowledgement before any resource accounting assumes it took effect.

This cannot recover work already spent decoding or eliminate the canonical next prompt's required prefill. Its possible benefit is avoiding useless transfers and cache competition. That makes other agents' latency and total completed work central measurements.

This is a candidate for testing. We have not established that current serving systems waste enough work this way, or that an existing policy fails to avoid it. Detecting the template behavior is already known; the possible contribution is a measured improvement in shared-server resource use with unchanged canonical prompts.

## Evidence needed, in order

1. **Representative sessions.** Start with a few genuine reasoning-enabled coding sessions across different tasks. Include an initial request and three to five useful follow-ups. Keep real tool waits and failures. ReedCode currently has no compaction, so obtain compaction traces from a harness that actually uses it or treat adding it as a separate change.
2. **A record of each context change.** Save messages before harness processing, the submitted request, template settings, server-rendered input fingerprints, and generated token IDs where available. Record user boundaries, re-serialization, compaction, routing, and cache events. An unexplained mismatch stays unknown. Multiple causes can overlap.
3. **Work that was actually wasted.** Measure which incompatible suffix blocks were still retained or copied for that session, which were also useful to other sessions, and which were evicted already. CPU rendering establishes compatibility; engine traces establish physical cost.
4. **A baseline worth beating.** First simulate admission on the captured traces. Compare decode writeback disabled, stock writeback enabled, and a fixed-prefix-only policy inspired by KVFlow. Keep ordinary caching and session-final cleanup consistent. Use identical model inputs, routing, memory budgets, and observed arrivals. If disabling writeback captures the same benefit, stop. Avoid manufacturing pressure solely to make the result positive.
5. **A held-out shared-server result.** Report completed tasks per GPU hour, fresh prefill work, cache memory-time, transferred bytes, and background p95 latency. Retain failures. Verify task quality and charge control overhead. Settings selected on exploratory runs stay fixed for the final comparison.

Stop if rewrites are rare in the target workload, if affected blocks are already reclaimed, if other sessions still need them, or if signaling costs erase the benefit. Keeping all reasoning is a separate comparison with its own quality and memory costs.

## Interpreting the pilot

The recorded prefill wall time is about 5.3% of summed request wall time. The candidate's 2.46% share of total continuation input uses a different denominator. Multiplying them does not establish a speedup bound: cached and fresh tokens have different costs, and batching changes those costs. The pilot still gives little reason to spend on the proposed quiet-workload optimization.

The proposed percentages for the chance of a breakthrough are subjective guesses. Multiplying them assumes calibrated conditional probabilities that we do not have. The stopping points above are the practical way to control the risk.
