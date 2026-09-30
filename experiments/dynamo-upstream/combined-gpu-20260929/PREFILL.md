# Controlled `speculative_prefill` comparison

All 18 planned trials completed: two saved sessions, three conditions, three repetitions. Stock warmups added work without increasing cache hits on the real requests. The fixed builder removed the separate warmup branch.

## Prefill and reuse

Counts were identical across all three repetitions of each cell. Scheduled prefill comes from the per-request change in `vllm:prompt_tokens_by_source_total{source="local_compute"}`, with matching process identity, one finished request and idle endpoints. Cached tokens come from response usage.

| Session | Condition | Scheduled prefill tokens, whole session | Cached tokens, real follow-ups | Retained warmup-only full blocks at end |
| --- | --- | ---: | ---: | ---: |
| Short | Off | 5,205 | 9,536 | 0 |
| Short | Stock | 8,446 | 9,536 | 200 |
| Short | Fixed | 5,240 | 9,904 | 0 |
| Long | Off | 20,580 | 35,264 | 0 |
| Long | Stock | 38,114 | 35,264 | 1,095 |
| Long | Fixed | 20,595 | 35,328 | 0 |

Stock added 3,241 computed tokens in the short session (+62.27%) and 17,534 in the long session (+85.20%) relative to off. The fix reduced whole-session prefill relative to stock by 37.96% and 45.96%. It remained slightly above off: 35 and 15 tokens.

The stock branch reused its own previous warmups. In the long session, the first warmup computed 17,452 tokens and the second computed 82. The branch provided no additional hits to the real follow-ups in these sessions.

## Parallel branch size

The selected engine used Qwen3-8B at the pinned revision, BF16, TP1, FlashAttention 2 and 16-token blocks. The model geometry gives 2.25 MiB of KV payload per full block. The engine reported 21,691 pool blocks; its startup capacity and every measured cache-configuration sample agree.

| Session | Final warmup-only payload | Current real-context full-block payload | Branch / current context |
| --- | ---: | ---: | ---: |
| Short | 450 MiB | 722.25 MiB | 62.31% |
| Long | 2,463.75 MiB | 2,889 MiB | 85.28% |

Just before the final tool output extended each real context, the branch reached 97.56% and 99.10% of its current full-block payload. The fixed and off conditions retained zero warmup-only blocks. The allocation section of [summary.json](prefill/results/summary.json) records the selected epoch, geometry, source hashes and derivation.

## Timing

Means across three repetitions, in milliseconds:

| Session | Condition | Sum of real follow-up request times | Sum of all request times | Replay elapsed time |
| --- | --- | ---: | ---: | ---: |
| Short | Off | 429.948 | 469.818 | 1,769.615 |
| Short | Stock | 429.662 | 753.523 | 1,948.310 |
| Short | Fixed | 405.866 | 542.465 | 1,764.634 |
| Long | Off | 436.883 | 2,051.882 | 2,327.658 |
| Long | Stock | 435.476 | 3,676.916 | 4,050.729 |
| Long | Fixed | 419.858 | 2,120.663 | 2,499.127 |

[summary.json](prefill/results/summary.json) retains all per-repetition values, paired differences and 108 measured request rows. Reproduction also generates `prefill/report.json`, with exact token linkage and every KV snapshot.

## Live path check and earlier attempt

A generated tool-call smoke exercised each actual hint path before the successful replay. Stock warmed 65 tokens and diverged from its real continuation at token 18. The fixed warmup's 220 tokens were an exact prefix of the 243-token continuation.

The earlier engine epoch passed its generated-tool smokes. The measurement identity endpoint then returned HTTP 503 because the backend launcher had been reparented to PID 1. No replay model request was sent. Its raw records and the `previous_failed_attempt` entry in [summary.json](prefill/results/summary.json) remain included, separate from the 18 successful cells.

## Limits

This is a controlled builder replay with one discarded generated token per request. The original assistant's decode cache is absent. Each warmup finishes before its follow-up; tool waits and observation overhead remain in elapsed time. These timings describe this serial replay. Agent quality, shared-server effects, eviction pressure and throughput were unmeasured.

Block payload is derived from published full-prefix entries and the selected engine configuration. The pool was already allocated. Exact physical allocations, partial blocks, metadata and allocator overhead were unmeasured. The pinned allocation source was checked; the installed GPU source files were not independently rehashed against it.

## Evidence and reproduction

The selected epoch is `267e605d0db348158ba0f551f5ac87e8`. Its stopped PCAP captured 4,532 packets with zero drops. Decoder linkage uses the epoch frontend log plus the 18 individual trial frontend logs and the verified local address `172.24.0.2`. All 1,519 files in the [final raw archive](raw/pod.tar.gz) passed manifest verification. The study manifest records its archive hash.

Use the [combined reproduction command](README.md#reproduce-locally). It checks every raw manifest entry, decodes the PCAP and rebuilds all 18 cells. The output includes the full report, decoded wire and KV geometry calculation.
