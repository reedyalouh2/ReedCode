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

All were supported in 1.5.0. [Original Claude configuration](operations/recommended-config-manifest.json), [Codex configuration](operations/codex-recommended-config-manifest.json) and [Claude YaRN configuration](operations/claude-yarn-config-manifest.json) record the evidence. Codex's frontend flags come from its recorded launcher and hash-matched source; its worker argv was captured live. Both Claude configurations include live frontend argv.

The 64K rerun kept Claude's original 8,192 output and 4,096 thinking budgets and used the [documented Qwen scaling](https://huggingface.co/Qwen/Qwen3-8B/blob/b968826d9c46dd6066d109eabc6255188de91218/README.md):

```text
--max-model-len 65536 --hf-overrides '{"rope_scaling":{"rope_type":"yarn","factor":2.0,"original_max_position_embeddings":32768}}'
```

The [manifest](manifest.json) records both image digests, model hashes, patches, commands and evidence hashes. The deployed image changed only the OCI runtime user from `dynamo` to `0`; all 67 filesystem layers match the recorded parent. Model weights and templates were unchanged.

## Run record

The original Claude session produced four successful requests, then three context-limit errors including its two follow-up attempts. Those records remain in the parity report. Codex completed next, followed by all 18 prefill trials and the Claude YaRN retry. The successful parity sessions kept follow-up gaps below ten seconds.

Parity used first-request `cached_tokens=0` as its clean-start evidence because the expected reset trace was unavailable. Every measured session has that explicit zero, plus the saved reset acknowledgement and canary evidence. The prefill comparison separately verified an empty router index through its clear-event counters.

Fixed frontend RPC-port registration failed during setup. Dynamic port selection worked without a serving-code patch. This is recorded as [candidate #6](../006-fixed-rpc-registration/README.md) for a separate reproduction and issue search. An earlier prefill attempt stopped at our identity monitor before sending any replay model request; its records remain in the archive.

The study ran for 79.43 minutes and cost an estimated **$2.37**, including disk and the earlier failed rental. [Run and cost record](operations/billing.json).

## Reproduce locally

From the repository root, choose a fresh output directory:

```bash
uv run --no-project --with msgpack==1.1.1 --with xxhash==3.5.0 python \
  experiments/dynamo-upstream/combined-gpu-20260929/reproduce.py \
  --output /tmp/reedcode-combined-reproduced
```

This verifies the packaged evidence and analysis code, safely extracts the unchanged 1,519-file pod archive, rebuilds all 18 prefill trials and all three parity cases, and checks them against the saved reports. It makes no model or provider requests. The separate client archive retains all 145 original client files, including failed responses and controller-budget attempts.

The project test suite passed: 190 executed, 26 skipped, 216 discovered. [Test log](verification/unit-tests.txt) and [environment record](verification/diagnosis.json). The decoder, reset, controller and analysis checks are recorded alongside their source.

## Limits and next step

These are single-GPU measurements with instrumentation enabled. They do not measure shared-server latency, throughput, eviction pressure or model quality. Prefill replays use one discarded generated token per request; their timing is separate from the real harness sessions. The two completed parity sessions use different context configurations and stay separate. Old-history suffix lengths show where reuse becomes impossible; they do not quantify a quality-preserving fix's savings. The cost estimate includes reserves; provider settlement was still unknown.

The result supports reviewing the [speculative-prefill issue draft](../001-speculative-prefill/issue-draft.md) and [tested patch](../001-speculative-prefill/fix/README.md). The compatible-prefix parity track stops here with no significant gap in the two completed sessions. The [current issue check and patch split](../001-speculative-prefill/upstream-split.md) describe the contribution planned for upstream review.
