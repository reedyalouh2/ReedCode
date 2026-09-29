# The parallel warmup branch

I counted the distinct full input blocks left by real requests and stock warmups in each recorded session. If every warmup completes and all blocks stay cached, the warmup branch adds **60.7–64.7% of the final real context's KV size**. The median is **62.1%**. Earlier in these sessions, that ratio peaks at **95.5–99.5%**.

This is CPU accounting of retained input blocks under those assumptions. Physical residency, evictions and latency still need a server measurement. The five hint-off sessions have hypothetical warmups throughout.

## Recorded sessions

These are the same 42 tool continuations from the [session-cost report](../session-cost/README.md). Its native rendering matched all 52 captured normal requests against the server fingerprints. This script reads those saved token IDs and verifies their hashes before counting anything.

The final snapshot includes the last observed real request. It excludes the final assistant's warmup because that response has no recorded continuation. Each session is treated separately.

| Session | Source hint | Final real input tokens | Final real full blocks | Warmup-only full blocks | Warmup-only KV, MiB | Added KV / final real KV |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| r01 | on | 5,150 | 321 | 200 | 450.00 | 62.31% |
| r01 | off | 5,413 | 338 | 218 | 490.50 | 64.50% |
| r02 | off | 5,407 | 337 | 218 | 490.50 | 64.69% |
| r02 | on | 5,162 | 322 | 199 | 447.75 | 61.80% |
| r03 | off | 5,097 | 318 | 194 | 436.50 | 61.01% |
| r03 | on | 5,143 | 321 | 198 | 445.50 | 61.68% |
| r04 | off | 5,110 | 319 | 196 | 441.00 | 61.44% |
| r04 | on | 5,102 | 318 | 193 | 434.25 | 60.69% |
| r05 | on | 5,162 | 322 | 202 | 454.50 | 62.73% |
| r05 | off | 5,202 | 325 | 205 | 461.25 | 63.08% |

Earlier real inputs leave zero to two additional full blocks outside the final real context. Those are recorded separately from warmup-only blocks. The warmup branch reaches its largest absolute size by the final snapshot in all ten sessions. Its largest ratio to the current real context occurs earlier, before the real context grows to its final length.

## Growth controls

The [existing controls](../session-cost/README.md#growth-with-context-length) append repeated ` trace` text to captured content, then render two consecutive warmups and follow-ups. They are constructed inputs; no long agent session was generated.

| Added text location | First real input tokens | Final real input tokens | Final real full blocks | Warmup-only full blocks | Warmup-only KV, GiB | Added KV / final real KV |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Earlier tool output | 4,096 | 4,510 | 281 | 260 | 0.5713 | 92.53% |
| Earlier tool output | 8,192 | 8,606 | 537 | 516 | 1.1338 | 96.09% |
| Earlier tool output | 16,384 | 16,798 | 1,049 | 1,028 | 2.2588 | 98.00% |
| Earlier tool output | 32,768 | 33,182 | 2,073 | 2,052 | 4.5088 | 98.99% |
| Earlier tool output | 50,000 | 50,414 | 3,150 | 3,129 | 6.8752 | 99.33% |
| System content | 50,000 | 50,414 | 3,150 | 183 | 0.4021 | 5.81% |

The long historical-tool control approaches a second context's worth of retained KV. Successive warmups share that branch, so its size does not grow by another full context on every call. Moving the added text before the schema mismatch sharply reduces the separate branch. All six controls retain one older real-input block outside the final real context.

## What the calculation counts

At each step, `R` is the union of completed real-input blocks, `W` is the union of completed warmup-input blocks, and `C` is the current real input's full blocks. The script takes a snapshot after each warmup and again after its following real request.

| Quantity | Formula |
| --- | --- |
| Warmup-only content | `W − R` |
| Older real content outside the current context | `R − C` |
| Combined retained content | `R ∪ W` |
| Warmup overhead relative to the current context | `len(W − R) / len(C)` |
| Combined content relative to the current context | `len(R ∪ W) / len(C)` |

Each 16-token block is identified by both its tokens and its parent block. An identical token sequence after a divergent prefix remains a different block. The script uses exact tuple identities rather than a probabilistic hash for this count. It deduplicates repeated warmups, includes real history only when that request occurs, and counts no future input early.

The [pinned official Qwen3-8B config](https://huggingface.co/Qwen/Qwen3-8B/blob/b968826d9c46dd6066d109eabc6255188de91218/config.json) has 36 full-attention layers, 8 KV heads and a head dimension of 128. For BF16 K and V on one unsharded GPU:

```text
bytes per token = 2 × 36 × 8 × 128 × 2 = 147,456 bytes = 144 KiB
bytes per block = 16 × 147,456 = 2,359,296 bytes = 2.25 MiB
```

Every ratio uses full blocks for both numerator and denominator. Remaining partial input tokens are listed separately in [results.json](results.json). This full-block content count differs from the prefix lookup's last-token rule: a block can be resident even when the engine must recompute input to obtain the next logits.

## Reproduce

From the repository root, choose a new output directory:

```bash
uv run python experiments/dynamo-upstream/001-speculative-prefill/kv-footprint/reproduce.py \
  --output /tmp/reedcode-kv-footprint-rerun
uv run python -m unittest tests/test_dynamo_kv_footprint.py
```

No network, GPU or model request is needed. The script checks the pinned session report, both compressed native evidence files and the saved official [model configuration](model-config.json). The result records their hashes and the script hash. Tests cover parent-dependent identity, duplicate blocks, stale real history, future-input leakage, partial blocks and the BF16 size formula.

## Limits

The calculation starts each session cold and assumes completed warmups, no eviction, no cancellation and unchanged cache identity. It excludes partial blocks, sampled decode tokens, allocator metadata, temporary workspace and unused preallocated cache. Those omissions mean it does not estimate total allocated GPU memory.

Cross-session sharing is excluded. Adding the isolated-session counts would not establish the occupancy of concurrent agents sharing a server. The recorded workload is short and has thinking disabled. The largest length controls exceed the original deployment's 32,768-token limit. Actual cache pressure and harm to other requests remain unmeasured.
