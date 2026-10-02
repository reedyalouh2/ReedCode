# Dynamo GPU check, September 29, 2026 UTC

The speculative-prefill bug produced measurable wasted work. Across two controlled sessions, stock warmups added 62.27% and 85.20% more prefill than leaving the hint off, with no extra cache hits on real follow-ups. The fix removed the separate warmup branch. [Prefill results](PREFILL.md).

Stock Dynamo also completed real Codex and Claude Code sessions, each with 15 model calls and two user follow-ups. Neither missed a compatible cached prefix. Claude needed a 64K YaRN retry after its original 32K session exceeded the context limit. [Parity results and causes](PARITY.md).

| Completed parity session | Actual cached / input | Input-only ideal / input | Actual / ideal |
| --- | ---: | ---: | ---: |
| Codex, 32K | 88.39% | 85.94% | 102.85% |
| Claude Code, 64K YaRN | 90.41% | 90.41% | 100.00% |

Codex reused previously generated tokens beyond the input-only reference. When those captured output IDs enter the reference, actual reuse matches it on every request in both sessions. History still changed across user turns: Qwen's template stripped earlier reasoning in Codex, and Claude Code removed reminder text on resume. These changes reduced the compatible prefix before cache lookup.

## Configuration

One A100-SXM4-80GB served `Qwen/Qwen3-8B` at revision `b968826d9c46dd6066d109eabc6255188de91218`, BF16, TP=1, prefix caching enabled, 16-token blocks. The backend was Dynamo 1.5.0 with vLLM 0.28.0. Parity used its unchanged frontend. The prefill comparison used matched stock and fixed frontends from main `f5d3353e2167bb0f0d729085eb5bc9183bf4b222`, with thinking disabled in both.

All measured parity sessions used these [recommended harness flags](https://docs.nvidia.com/dynamo/dev/digest/agentic-harnesses):

```text
frontend: --enable-anthropic-api --strip-anthropic-preamble --enable-streaming-tool-dispatch
worker:   --dyn-tool-call-parser hermes --dyn-reasoning-parser qwen3 --dyn-default-thinking-mode enabled
```

All were supported in 1.5.0. The [Claude configuration](configuration/recommended-config-manifest.json), [Codex configuration](configuration/codex-recommended-config-manifest.json) and [Claude YaRN configuration](configuration/claude-yarn-config-manifest.json) record the commands. Codex's frontend flags come from its hash-matched launcher; its worker argv was captured live. Both Claude configurations include live frontend argv.

The 64K rerun kept Claude's original 8,192 output and 4,096 thinking budgets and used the [documented Qwen scaling](https://huggingface.co/Qwen/Qwen3-8B/blob/b968826d9c46dd6066d109eabc6255188de91218/README.md):

```text
--max-model-len 65536 --hf-overrides '{"rope_scaling":{"rope_type":"yarn","factor":2.0,"original_max_position_embeddings":32768}}'
```

The [manifest](manifest.json) records both image digests, model hashes, patches, commands and evidence hashes. The deployed image changed only the OCI runtime user from `dynamo` to `0`; all 67 filesystem layers match the recorded parent. Model weights and templates were unchanged.

Parity used first-request `cached_tokens=0` as its clean-start evidence because the expected reset trace was unavailable. Reset acknowledgements and canaries remain in the raw capture. The prefill comparison verified an empty router index through its clear-event counters.

The study ran for 79.43 minutes and cost an estimated **$2.37**, including disk and the earlier failed rental. The [manifest](manifest.json) records the calculation.

## Reproduce locally

From the repository root, choose a fresh output directory:

```bash
uv run --no-project --with msgpack==1.1.1 --with xxhash==3.5.0 --with httpx==0.28.1 python \
  experiments/dynamo-upstream/combined-gpu-20260929/reproduce.py \
  --output /tmp/reedcode-combined-reproduced
```

This checks the evidence hashes, extracts the unchanged 1,519-file pod archive, and rebuilds all 18 prefill trials and all three parity cases. It compares the output with the saved results without making model or provider requests. The separate client archive retains all 145 original files, including failed responses and requests stopped at the session limit. Full token joins, KV snapshots and decoded captures are generated in the output directory.

## Limitations

These are single-GPU measurements with instrumentation enabled. They do not measure shared-server latency, throughput, eviction pressure or model quality. Prefill replays use one discarded generated token per request; their timing is separate from the real harness sessions. The two completed parity sessions use different context configurations and stay separate. Old-history suffix lengths show where reuse becomes impossible; they do not quantify a quality-preserving fix's savings. The cost estimate includes reserves; provider settlement was still unknown.
