# How much useful context can we prepare?

I checked every captured coding trajectory from the September 28 GPU pilot: ten runs of `noisy-bugfix`, 52 model calls, and 42 tool continuations. The corrected prefix adds a median of **80 tokens** beyond the complete blocks that match the preceding input. The range is 16–256 tokens. Almost all earlier input survives the conversation boundary.

This is a new analysis of saved GPU traces. It made no model requests. [trajectory-results.json](trajectory-results.json) contains every request, transition, and run, including the source hashes.

## Results

| Check | Result |
| --- | ---: |
| Requests matching their server input fingerprints | 52/52 |
| Recorded input hashes checked, including partial blocks | 9,703 |
| Continuations with an exact candidate prefix | 42/42 |
| Prior complete-block tokens still matching the next input | 102,512 / 102,592 (99.92%) |
| Continuations losing a complete block of prior input | 5/42, one block each |
| Extra complete-block tokens the candidate can prepare | 3,712 total; median 80; range 16–256 |
| Tool waits | Median 237.885 ms; range 98.96–354.38 ms |

Here is how the 151,182 input tokens across the 42 continuations break down. These totals count prompt positions each time a request occurs.

| Part of the continuation input | Tokens | Share |
| --- | ---: | ---: |
| Complete matching blocks from the preceding input | 102,512 | 67.81% |
| Additional complete blocks in the prepared candidate | 3,712 | 2.46% |
| Everything after the candidate | 44,958 | 29.74% |

The last row includes tool results, message framing, and partial-block slack. The tool-result data arrives when execution finishes in this harness; some framing and slack are already known. The preparation candidate uses only the history and completed assistant message available before the tool returns.

The Qwen boundary mismatch occurs inside the preceding input in all 42 transitions. Because the mismatch comes before generated output, the missing raw sampled token IDs do not affect this particular reuse bound. The candidate repairs that boundary and includes the completed assistant's tool call.

The renderer also passes a broader check against all 314 saved server requests: 46,759 matching hashes. This includes the pilot, replay, and diagnostic requests. Tool argument strings and tool-schema key order must stay intact when rebuilding their prompts.

## What this changes

The proposed contention study on these ten trajectories has been dropped. These prompts and tool waits give little reason to expect useful eviction during a wait. The traces remain available as a regression workload.

## Limits

These are ten trajectories of one easy synthetic task. All ten passed its verifier. The prompts reach at most 5,413 tokens, and the longest tool wait is 354.38 ms. They provide a small control workload for the next study.

Matching blocks measure token compatibility. Cache residency and saved compute need server evidence. Cache carried between the original trials: 9/10 initial requests already had hits, and 15/42 continuations had more hits than the preceding input alone explains. The original off/on labels are preserved as provenance only in this survey.

The saved server traces report 2,779.119 ms of prefill time and 52,005.961 ms of total request time across the 52 calls. These are descriptive wall-time fields. They do not provide a causal speedup estimate, GPU kernel time, or a valid whole-epoch counter comparison. No new latency comparison was run.

## Reproduce

Download the pinned tokenizer using the [prefix audit instructions](README.md#run-locally), then run:

```bash
uv run --with-requirements experiments/dynamo-prefix/requirements.txt \
  python experiments/dynamo-prefix/survey.py \
  --tokenizer /tmp/reedcode-prefix-tokenizer/tokenizer.json \
  --output /tmp/trajectory-results.json \
  --export-workload /tmp/trajectory-workload.json
```

The script verifies the archive and trace checksums, joins each model call to its server record, and checks rendered input hashes. Export requires complete coverage and normally completed trajectories. A partial or failed record stays visible in the report and prevents exporting it as the complete workload. The output is compatible with `dynamo_replay.py`; preparation policies still need to be added to that runner.
