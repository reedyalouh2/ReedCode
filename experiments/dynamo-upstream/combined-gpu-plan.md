# GPU study method

Completed September 29, 2026 UTC. The [results](combined-gpu-20260929/README.md) cover 18 prefill trials, Codex at 32K, and Claude at 64K YaRN after the original 32K attempt failed. This page records the comparison design. Actual launch settings and deviations are in the study manifest.

The run used one A100-SXM4-80GB and cost an estimated $2.37, including disk and the earlier failed rental.

These are separate serving configurations on the same GPU:

| Study | Frontend | Thinking | Hints |
| --- | --- | --- | --- |
| Speculative-prefill control | Unpatched main `f5d3353e2167bb0f0d729085eb5bc9183bf4b222` | Disabled | Off, then stock hint |
| Speculative-prefill fix | Same main revision plus the two local patches | Disabled | Fixed hint |
| Parity Run 1 | Recorded stock 1.5.0 image, embedded revision `32b8b2f8c63fa3531c34b64c1cf2cbe39a6f9653` | Enabled | None |

The stock-versus-fix comparison keeps the revision matched. Parity never uses the patched frontend. Restart with the appropriate configuration and clear cache state between studies; never compare a 1.5.0 baseline against a patched main and call the difference a fix effect.

The [CPU patch checks](001-speculative-prefill/fix/README.md) are complete: 54/54 exact prefixes, unchanged normal requests, a passing Dynamo test-target compile and 19 passing warmup-module tests. The [branch-size report](001-speculative-prefill/kv-footprint/README.md) supplies the CPU estimate to compare with actual residency.

## Speculative-prefill comparison

Compare **hint off**, **stock hint**, and **fixed hint**. Use three repetitions per session and rotate the order across repetitions: off/stock/fixed, stock/fixed/off, fixed/off/stock. Use the same arrival delays and output limits. All instrumentation stays on in every condition. The first patch supports default routing only; leave worker targeting, cache namespaces, LoRA, priority and other routing hints unset in the matched experiment. Record any changes made to archived request metadata. Confirm that a fixed supported tool turn actually dispatches a warmup before measuring its reuse.

Each repetition starts from an empty backend cache and router index. Validate the reset procedure during preflight; prefer a verified cache reset over reloading the model for every condition. Keep the cache between turns within a session. Verify the first request has zero cached tokens and that cache-event tracking starts from a known empty state. A fresh first-block marker can isolate reuse but cannot prove equal cache occupancy, so it cannot replace this reset for footprint measurements.

First verify the end-to-end stock and fixed hint paths with an actual generated tool call. Save the returned assistant, the internal warmup and the real tool-result continuation. Compare the exact request and assistant token sequences across conditions. If generation changes the trajectory, report it and exclude that pair from the fixed-input latency comparison; retain all failures and skip counts.

For the controlled cache comparison, replay the same saved assistant and continuation through a test seam that invokes the stock or fixed request builder. It must use that builder's exact token IDs and the real backend. Label this as a controlled builder replay. It measures the rendering change while the end-to-end check establishes that the hint calls the builder correctly. Keep warmup completion explicit before the follow-up in the primary comparison. An additional replay with recorded tool delays can measure cancellation and overlap if time remains.

A fix may skip an unsafe warmup. Count that outcome. Its benefit would be avoiding the incompatible branch; it should not be described as improved preparation or extra cache reuse.

Record, per request and per warmup:

- Parent request ID, condition, trial, token hashes, completion/cancellation/skip reason, and backend process identity.
- Actual scheduled prefill tokens and cache-hit tokens. Validate their definitions against the pinned backend. A total prompt-token counter includes reused tokens and cannot measure recomputation.
- KV `Stored` and `Removed` events, complete token-chain hashes, and a reconstructed resident-block set. Repeated publication does not mean a second allocation. A gap or restart invalidates resident-size attribution until the cache is reset. Events describe cacheable full blocks; report active partial-block allocations separately if the backend exposes them.
- Warmup-only resident blocks, blocks shared with the real history, and useful real blocks removed. Report branch bytes divided by the contemporaneous real-context bytes. Also report unique retained history so stale real branches are visible. Verify bytes per block from the engine's actual allocation; the CPU BF16 estimate alone cannot establish physical residency.
- Follow-up cached tokens, time to first token, completion latency and total session time. Preserve every raw repetition and the paired differences.

Three repetitions give a small validation study. Avoid a stable tail-latency or throughput claim. If an actual-compute counter or parent linkage is unavailable, report that limitation and the token/event evidence separately. Do not infer those measurements from CPU accounting.

The correctness check passes when the stock mismatch reproduces, each supported fixed warmup matches the real continuation prefix, unsupported cases skip, and the patch leaves the ordinary request tokens unchanged. Speed and memory are measured outcomes; a null latency result does not erase a verified rendering correction.

The shared-server contention check stays queued for a separate proposal. This rental reserves its remaining time for the two real harness sessions and teardown.

## Parity Run 1

Run one Claude Code session through `/v1/messages` and one Codex session through `/v1/responses`. Both use the same public ReedCode commit and three frozen user messages: a coding change, a test follow-up and a behavior refinement. Each has its own cold cache start. Use normal client continuation between turns, with short measured delays. Full details and exact prompts are in [Parity Run 1](parity-run-1/README.md).

Save raw harness bodies, backend input IDs, responses, final prompt/cached usage, KV events and timing. Report actual reuse, the requested input-prefix ideal and their signed difference. Show initial, within-turn and cross-turn requests separately, plus the per-session ratio of total actual to total ideal.

The protocol includes one measurement clarification: rewritten history lowers the input-prefix ideal itself. Attribute compatible cache misses and history rewrites in separate ledgers so these costs are visible without double counting. Each counted range gets one primary cause; unresolved or interacting causes stay under `other`. Include earlier computed output in a supplementary reference when raw token/event evidence supports it.

Use three percentage points as the pilot's “few points” threshold for both clients, separately within-turn and cross-turn. A complete, attributable result within that threshold ends further parity GPU work with “no significant gap observed relative to the compatible-prefix ideal.” A low ideal, missing provenance or incomplete session cannot support a broader claim. Preserve the history-rewrite audit even if cache misses are small. The hosted 85–97% range is context only.

## Evidence

The [study directory](combined-gpu-20260929/README.md) contains the manifest, patch hashes, launch commands, raw traces, cache events, metrics and one-command reproduction.
