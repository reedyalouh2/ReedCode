# speculative_prefill drops tool and assistant fields, creating an unusable warmup branch

With `nvext.agent_hints.speculative_prefill=true`, Dynamo can prefill a different prefix from the next tool continuation. On Qwen3-8B, stock warmups added **62.27% and 85.20% more scheduled prefill** in two controlled sessions, without adding any cache hits to real follow-ups. A parallel warmup branch retained up to **2,463.75 MiB of full-block KV payload**.

The local fix removes that branch and reduces prefill **37.96% and 45.96% versus stock**. Counts repeated exactly across three repetitions per condition.

## Root cause

The current [request wrapper](https://github.com/ai-dynamo/dynamo/blob/51b83df91f3fca2f52ed644f744c4465c3d163b3/lib/llm/src/preprocessor/speculative_prefill.rs#L109) carries only messages. Tools and effective template settings are lost. The [stream accumulator](https://github.com/ai-dynamo/dynamo/blob/51b83df91f3fca2f52ed644f744c4465c3d163b3/lib/llm/src/preprocessor/speculative_prefill.rs#L285) collects text alone, so the assistant appended by the warmup lacks its tool calls and separate reasoning.

That changes the rendered prefix before most of a tool-using agent's context. Qwen has a second mismatch: the final assistant in a warmup renders differently from the same assistant followed by a tool result. The isolated tool and text probes first diverge at token 40 and 56 respectively. The [token-level reproduction](https://github.com/reedyalouh2/ReedCode/blob/1caea0d2d5538dd66e3e47cb4b6963e4c3ff329b/experiments/dynamo-upstream/001-speculative-prefill/README.md) records the exact causes.

On September 29, main was `51b83df91f3fca2f52ed644f744c4465c3d163b3`. Its speculative-prefill module is byte-identical to the one tested at `f5d3353e2167bb0f0d729085eb5bc9183bf4b222`. [Current source check](https://github.com/reedyalouh2/ReedCode/blob/1caea0d2d5538dd66e3e47cb4b6963e4c3ff329b/experiments/dynamo-upstream/001-speculative-prefill/recheck-20260929/README.md).

## Reproduce

The [two-request live probe](https://github.com/reedyalouh2/ReedCode/blob/1caea0d2d5538dd66e3e47cb4b6963e4c3ff329b/experiments/dynamo-upstream/001-speculative-prefill/README.md#repeat-on-a-server) sends the public hint, saves the returned assistant, then appends a matching tool result with the same schema. It captures the internal warmup and real follow-up. There is no client preparation adapter.

To verify the recorded GPU comparison locally:

```bash
git clone https://github.com/reedyalouh2/ReedCode.git
cd ReedCode
git checkout 1caea0d2d5538dd66e3e47cb4b6963e4c3ff329b
uv run --no-project --with msgpack==1.1.1 --with xxhash==3.5.0 --with httpx==0.28.1 python \
  experiments/dynamo-upstream/combined-gpu-20260929/reproduce.py \
  --output /tmp/reedcode-combined-reproduced
```

This verifies the raw hashes, decodes backend token IDs, reconstructs the KV events, and checks all 18 trial results. No GPU is needed to reproduce the analysis. [Raw captures and manifest](https://github.com/reedyalouh2/ReedCode/blob/1caea0d2d5538dd66e3e47cb4b6963e4c3ff329b/experiments/dynamo-upstream/combined-gpu-20260929/README.md).

## GPU result

One A100-SXM4-80GB, Qwen3-8B at `b968826d9c46dd6066d109eabc6255188de91218`, BF16, TP=1, 16-token blocks, prefix caching enabled and thinking disabled. Stock and fixed frontends were built from the same main revision, `f5d3353`, against the same Dynamo 1.5.0 / vLLM 0.28.0 backend. [Image digests, launch commands and artifact hashes](https://github.com/reedyalouh2/ReedCode/blob/1caea0d2d5538dd66e3e47cb4b6963e4c3ff329b/experiments/dynamo-upstream/combined-gpu-20260929/manifest.json).

Two saved sessions, off/stock/fixed, three repetitions with rotated order:

| Session | Condition | Scheduled prefill tokens | Real follow-up cached tokens | Retained warmup-only blocks |
| --- | --- | ---: | ---: | ---: |
| Short | Off | 5,205 | 9,536 | 0 |
| Short | Stock | 8,446 | 9,536 | 200 |
| Short | Fixed | 5,240 | 9,904 | 0 |
| Long | Off | 20,580 | 35,264 | 0 |
| Long | Stock | 38,114 | 35,264 | 1,095 |
| Long | Fixed | 20,595 | 35,328 | 0 |

Scheduled prefill is the per-request delta of `vllm:prompt_tokens_by_source_total{source="local_compute"}`, with unchanged process identity and one finished request. Backend token arrays match the replay inputs. Cache resets bracket each trial; the KV event ledger reconstructs retained full blocks.

Stock warmups reuse their own branch. In the long session, the first computes 17,452 tokens and the second computes 82. Neither increases real follow-up cache hits. At the end, the branch represents 450 MiB and 2,463.75 MiB: 62.31% and 85.28% of the respective real contexts' full-block payload. [Per-request evidence and timings](https://github.com/reedyalouh2/ReedCode/blob/1caea0d2d5538dd66e3e47cb4b6963e4c3ff329b/experiments/dynamo-upstream/combined-gpu-20260929/PREFILL.md).

Before replay, generated-tool smokes exercised the actual stock and fixed hint paths. Stock warmed 65 tokens and diverged at token 18. The fixed 220-token warmup was an exact prefix of the 243-token continuation.

## Fix and related work

The prototype preserves the effective request and complete assistant, then uses an explicit renderer continuation hook. It passes 54/54 prefix cases, leaves ordinary request tokens unchanged, compiles the patched Dynamo test targets and passes all 19 warmup-module tests. [Patch and tests](https://github.com/reedyalouh2/ReedCode/blob/1caea0d2d5538dd66e3e47cb4b6963e4c3ff329b/experiments/dynamo-upstream/001-speculative-prefill/fix/README.md).

The proposed contribution separates request/assistant preservation from the renderer hook. In the [CPU ablation](https://github.com/reedyalouh2/ReedCode/blob/1caea0d2d5538dd66e3e47cb4b6963e4c3ff329b/experiments/dynamo-upstream/001-speculative-prefill/scope/README.md), restoring fields produces exact tool prefixes for the tested Nemotron and GPT-OSS cases. Qwen's empty-reasoning case still needs the boundary change.

[#12109](https://github.com/ai-dynamo/dynamo/pull/12109#issuecomment-5073755624) already discusses speculative-rendering parity. It and [#12204](https://github.com/ai-dynamo/dynamo/pull/12204) closed without merging on September 29, citing [#12332](https://github.com/ai-dynamo/dynamo/pull/12332) and [#12333](https://github.com/ai-dynamo/dynamo/pull/12333). Those merged changes cover argument normalization and truncated-call recovery; neither changes this warmup builder. [Search and final-diff check](https://github.com/reedyalouh2/ReedCode/blob/1caea0d2d5538dd66e3e47cb4b6963e4c3ff329b/experiments/dynamo-upstream/001-speculative-prefill/recheck-20260929/README.md).

## Limitations

The GPU comparison is a serial builder replay with one discarded generated token per request. The original assistant decode cache is absent, and warmups finish before their follow-ups. Timings include instrumentation and tool waits. KV payload is derived from full blocks in a preallocated pool; exact allocator overhead is unmeasured. Concurrent-serving latency, eviction pressure and task quality need separate experiments. Cross-model results are CPU rendering checks; the complete tested runtime fix supports the pinned Qwen tool template.
