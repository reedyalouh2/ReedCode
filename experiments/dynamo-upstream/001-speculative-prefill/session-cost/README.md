# How much input does the bad warmup create?

I replayed the stock rendering path on CPU for all 42 tool continuations in the ten recorded sessions. Every warmup differs from its actual follow-up at token 31. Across those warmups, 91,783 tokens follow the first mismatch.

That number overstates fresh work if earlier warmups remain cached. Successive warmups share much of the same incorrectly rendered history. An input-only cache model reduces the uncovered input to 32,733 tokens across the 42 calls. This is a cache-accounting estimate; the GPU has not measured either cost.

## Recorded sessions

The native runner uses the unchanged request builder from Dynamo main `f5d3353e2167bb0f0d729085eb5bc9183bf4b222`, renderer 5.4.0, and the pinned Qwen3-8B tokenizer. All 52 unique normal requests match the archived server fingerprints: 9,703 hashes checked. The [build record](../current-code/README.md) pins the compiler, dependencies and source fragments.

| Quantity | Hint-on source sessions | Hint-off source sessions | Total |
| --- | ---: | ---: | ---: |
| Tool continuations | 20 | 22 | 42 |
| Warmup input tokens | 43,083 | 50,002 | 93,085 |
| Tokens after divergence from the next request | 42,463 | 49,320 | 91,783 |
| Tokens in full blocks that fail to match the next request | 42,576 | 49,472 | 92,048 |
| Uncovered input, using prior normal inputs only | 42,763 | 49,650 | 92,413 |
| Uncovered input, also retaining prior warmups | 16,059 | 16,674 | 32,733 |
| Uncovered full-block tokens, also retaining prior warmups | 15,872 | 16,496 | 32,368 |

Warmups for the hint-off sessions are hypothetical. Both columns use CPU reconstruction; they do not count backend executions or cache insertions. Earlier warmups add modeled coverage in 32 of the 42 transitions.

The block total includes the matching beginning of the block that contains the first mismatch. The suffix total starts at the exact mismatched token. Partial trailing blocks are recorded separately in [results.json](results.json).

Token 40 belongs to the shorter isolated tool example. The recorded sessions have a different system prompt, so the missing tool definitions cause divergence at token 31 instead. The cause is the same. Inputs before these tool continuations range from 381 to 3,524 tokens; the largest following request has 5,413.

## Growth with context length

I made length controls from two consecutive captured tool transitions. Each pair shares the same expanded historical tool result. The only added text is repeated ` trace`; no long session was generated. The two-step window starts cold at captured turn 2, retaining its original input before the first warmup.

| First normal input | First warmup input | First warmup tokens after divergence | First warmup uncovered input | Second warmup uncovered input |
| ---: | ---: | ---: | ---: | ---: |
| 4,096 | 3,874 | 3,843 | 3,858 | 319 |
| 8,192 | 7,970 | 7,939 | 7,954 | 319 |
| 16,384 | 16,162 | 16,131 | 16,146 | 319 |
| 32,768 | 32,546 | 32,515 | 32,530 | 319 |
| 50,000 | 49,778 | 49,747 | 49,762 | 319 |

The first warmup creates a long alternate prefix when the added history follows the missing schema. The second warmup can reuse it. Its 319 uncovered input tokens include the block boundary and trailing input under the residency assumptions below. They are not a measured prefill count.

Placement matters too. When the same 50,000-token target is reached by expanding the system text, divergence moves to token 47,152. Only 2,625 tokens remain after it. A long context alone does not predict the cost: the amount of history after the changed rendering matters.

## Accounting assumptions

Each recorded session starts with an empty cache. Before a warmup, the model can reuse complete 16-token blocks from earlier normal inputs, its current original input, and earlier warmup inputs. It retains every such block without eviction. All earlier warmups complete before the next one. Model and cache identity stay the same.

The script finds the longest matching prefix among those inputs and rounds down to full blocks. It leaves at least one input token to compute the next logits, following the pinned vLLM lookup. Future input is used only to compare prefixes and never enters the modeled cache early. The [router source audit](../router/README.md) documents the backend lookup and block lifecycle.

This model omits sampled decode-token IDs and cross-session hits. Eviction, cancellation, admission and overlapping execution can reduce the available coverage; other cached requests can increase it. These estimates are neither a measured cost nor a bound on actual GPU work.

## Reproduce

Build the [current-source runner](../current-code/README.md#reproduce) first. Then, from the repository root:

```bash
uv run --with-requirements experiments/dynamo-prefix/requirements.txt \
  python experiments/dynamo-upstream/001-speculative-prefill/session-cost/reproduce.py \
  --binary /tmp/reedcode-current-stock-prefill/main/reproduction-binary \
  --build-report /tmp/reedcode-current-stock-rerun.json \
  --tokenizer /tmp/reedcode-prefix-tokenizer/tokenizer.json \
  --output /tmp/reedcode-session-cost-rerun
```

Use the report from that build and a new output directory. The script verifies the source manifest, binary hash, upstream dependency pins, archived traces and server fingerprints. [results.json](results.json) includes the script and artifact hashes. [input.json.gz](input.json.gz) and [native-output.json.gz](native-output.json.gz) preserve all 54 case inputs and native outputs.

## Limits and next measurement

The ten terminal responses have no observed follow-up and are excluded. These records cover short, single-user-turn sessions with thinking disabled. The length controls are constructed and exceed the original server's 32,768-token limit at their largest sizes. They demonstrate rendering and cache-accounting behavior only.

Actual prefill work, block insertions, evictions and other agents' latency remain unmeasured. The pilot's 7.305 s versus 7.266 s does not provide reliable directional evidence. The next server test should distinguish a cold alternate prefix from repeated warmups that reuse that prefix, and compare hint-off, stock and a validated fix. GPU approval, a cap under $2 and key rotation are still required.
