# Dynamo GPU validation

I ran ReedCode against Dynamo on an A100 80GB on September 28, 2026 UTC. All ten coding trials passed. Replaying a captured session with four concurrent copies showed almost no change in completion time. A separate trace check found that the speculative tool-call prefix diverged from the next real request.

The pod cost about $0.50 and was deleted after the records were downloaded. Runpod reported $9.4991851972 remaining and $0/hour ongoing spend.

## What ran

The server used Dynamo 1.5.0, vLLM 0.28.0, and Qwen/Qwen3-8B in bfloat16, with thinking disabled. Prefix caching and KV events were enabled. The frontend used KV routing. The container digest, model revision, driver, and launch commands are in [deployment.json](deployment.json).

The coding pilot kept the first 20,000 characters of each tool result. It ran five interleaved off/on pairs on `noisy-bugfix`, with a 4,096-token response budget. The task oracle passed before the trials.

| Coding pilot | Off | On |
| --- | ---: | ---: |
| Verifier passes | 5/5 | 5/5 |
| Output-limit hits | 0 | 0 |
| Mean model calls | 5.4 | 5.0 |
| Mean tool calls | 4.4 | 4.0 |
| Mean model latency | 6.718 s | 6.406 s |

For replay, I captured the first scheduled coding trial. Each of ten interleaved epochs replayed its five requests in four concurrent workflows, keeping prompts and recorded tool waits fixed. Every workflow also sent a session-final request.

| Replay | Off | On |
| --- | ---: | ---: |
| Completed workflows | 20/20 | 20/20 |
| Mean workflow completion | 7.266 s | 7.305 s |
| Mean of epoch post-tool first-output p90s | 496.374 ms | 462.234 ms |
| Client requests, including session-final requests | 120 | 120 |
| Output-limit hits | 0 | 0 |

The mean paired completion difference was +39.162 ms with speculation on. All 40 session-final requests returned successfully. The [pilot report](pilot-report.md) and [replay report](replay-report.md) retain the paired differences.

## The prefix mismatch

After the timing runs, I enabled Dynamo's routing-hash logs and sent isolated text and tool-call continuations. Each conversation started with a fresh identifier. The first requests had zero cached input tokens.

In the tool-call case, Dynamo prepared a 72-token prompt. The actual follow-up had 244 tokens. Only the first two full 16-token blocks matched between them. The original request already contained all 12 blocks that the follow-up reused.

| Isolated check | Text continuation | Tool continuation |
| --- | ---: | ---: |
| Speculative prompt tokens | 64 | 72 |
| Full blocks in speculative prompt | 4 | 4 |
| Leading blocks shared with the next request | 3 | 2 |
| Next-request cached tokens, off | 48 | 192 |
| Next-request cached tokens, on | 48 | 192 |

Neither check gained a reusable full block from speculation. The server counters recorded two backend requests per client request with the hint on, compared with one when it was off.

The [source in the running image](https://github.com/ai-dynamo/dynamo/blob/32b8b2f8c63fa3531c34b64c1cf2cbe39a6f9653/lib/llm/src/preprocessor/speculative_prefill.rs) copies the original messages and accumulates assistant text. Its speculative request omits the tool definitions and the assistant's tool calls. That changes the rendered prompt before the tool result arrives. The text check also had a mismatched final block, which needs a separate look at Qwen's chat-template boundaries.

My next step is to preserve those fields and check the rendered prefix against the next request before testing a more ambitious scheduling policy.

## Measurement limits

This is one easy coding task and one captured replay workload. The server cache carried between conditions. Replay responses differed from the capture in 190 of 200 calls, including generated tool-call IDs. The timing results have no confidence interval or speedup claim. The isolated checks test the mechanism on this deployment, with debug logging enabled separately from the timing runs.

All ten replay epochs had idle boundaries, drained successfully, and had no scrape errors. Their metrics still failed the collector's restart check because the endpoint omitted `process_start_time_seconds`. The reports leave server phase, cache-occupancy, and counter comparisons unknown; the archive retains the raw values and process snapshots. Session-final delivery was verified, but a serving-side cleanup consumer was not tested. No GPU kernel time was measured.

## Records and reproduction

[results.json](results.json) contains the summary, routing hashes, and metric-validation flags. [raw-records.tar.gz](raw-records.tar.gz) contains the manifests, task snapshot, client traces, replay epochs, server logs, metric snapshots, launch scripts, and diagnostic scripts. These are synthetic prompts and public task code. Every archived file is checksummed.

From the repository root, recompute the summary and verify the archive:

```bash
python3 experiments/dynamo-20260928/summarize.py
```

The generated reports keep their automatic `unverified` hint label. The server-log checks above are separate evidence of actual preparation and prefix mismatch. The [run guide](../../docs/dynamo.md) describes the pilot and replay commands.
