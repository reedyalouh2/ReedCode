# speculative_prefill drops tool definitions and assistant fields, producing incompatible warmup prefixes

Local draft for review. Nothing has been filed.

## Problem

With stock `nvext.agent_hints.speculative_prefill=true`, the internal warmup can render a different prefix from the next normal Chat request. On the recorded Qwen3 deployment, the tool case drops the tool schema and completed assistant tool call. A text-only control also differs because the assistant is rendered in a different template position.

The [request-extension reference](https://docs.nvidia.com/dynamo/agents/agent-hints) describes preparing the predicted next-turn prefix. The [1.5.0 implementation](https://github.com/ai-dynamo/dynamo/blob/b83b1d9304ebfc624709ac46db32b1b6f1ff1615/lib/llm/src/preprocessor/speculative_prefill.rs) carries only messages and accumulates assistant text. The client in this reproduction only sends the public hint; it does not submit an explicit preparation request.

## Reproduction and evidence

The September 28, 2026 deployment used one A100-SXM4-80GB, Dynamo 1.5.0, vLLM 0.28.0, BF16 Qwen/Qwen3-8B, thinking disabled, and prefix caching enabled.

- Container: `nvcr.io/nvidia/ai-dynamo/vllm-runtime@sha256:d18389c89eb319401fdd73f1fbbaff10d9382f634b879941dfba75bbf7260c1e`
- Dynamo image commit: `32b8b2f8c63fa3531c34b64c1cf2cbe39a6f9653`
- Model revision: `b968826d9c46dd6066d109eabc6255188de91218`

Send a request asking for a `record_value` tool call with its schema and the stock hint. Append the returned assistant message and a matching tool result, then send the follow-up with the same schema. The internal preparation completes before that follow-up. The evidence packet includes exact launch commands, wire requests, router hashes, and backend receipt/completion logs.

The [reproduction instructions](README.md#reproduce-the-saved-finding-on-cpu) verify the saved finding with one CPU command. The same page includes a two-request HTTP client for repeating the test on a server. [results.json](results.json) records the hashes and version pins; the [provenance manifest](provenance-manifest.json) maps the extracted evidence back to the original logs.

| Recorded case | Warm input | Follow-up input | First divergence, zero-based | Follow-up cached tokens, off/on |
| --- | ---: | ---: | ---: | ---: |
| Tool call | 72 tokens | 244 tokens | 40 | 192 / 192 |
| Text control | 64 tokens | 78 tokens | 56 | 48 / 48 |

For tools, the warm prompt closes the system message where the normal request includes `# Tools`. Its first two complete blocks match; the third differs. For text, the warm prompt includes an empty thinking wrapper before the historical assistant answer; the follow-up omits it.

The runtime captures input lengths and complete 16-token block hashes. Local reconstruction matches those hashes and identifies the exact token positions. The tool case's final eight prepared token IDs are outside the recorded full-block hashes. This distinction is retained in the report.

A [CPU rerun](current-code/README.md) on September 28 compiled unchanged builder fragments from the latest release, v1.5.0 (`b83b1d9304ebfc624709ac46db32b1b6f1ff1615`), and `main` (`f5d3353e2167bb0f0d729085eb5bc9183bf4b222`). With their pinned Rust renderers and tokenizers, both reproduce the same token-40 and token-56 mismatches. Normal requests match the saved server fingerprints in both builds. That CPU check executes the isolated rendering path. The September 29 GPU study below also exercises matched stock and patched main frontends against the pinned vLLM 0.28.0 backend.

Both checks gain zero **additional** matching full blocks from preparation. The original request already provides the shared prefix. Each on request also produces one extra completed backend request and one generated token in the isolated records; counter observations lack an automatic restart metric and are corroborated by request logs.

## Scope and potential cost

The [cross-template CPU check](scope/README.md) runs 16 authored cases against each revision. Missing tool definitions cause early divergence for Qwen3-8B, Nemotron-3-Nano and GPT-OSS-20B. Restoring the schema moves the divergence later. Restoring settings and the complete assistant then produces exact tool prefixes for the tested Nemotron and GPT-OSS cases. Qwen still has an empty-reasoning boundary mismatch. DeepSeek-R1 tool-follow-up rendering errors prevent its prefix comparison. Text-only boundaries also vary across these templates.

The [recorded-session reconstruction](session-cost/README.md) covers 42 tool continuations in ten Qwen sessions. It finds 91,783 tokens after the mismatch across 93,085 warmup input tokens. Every first difference is at token 31 because these sessions have a shorter system prefix than the isolated control.

Repeated warmups can reuse that incompatible history. An input-only model with all earlier normal and warmup inputs resident leaves 32,733 uncovered input tokens. In a constructed 50K historical-tool-output control, the first warmup leaves 49,762 uncovered input tokens; its successor leaves 319. These controls establish a potentially large cold-prefix cost and substantial repeat reuse. They do not measure executed prefill, cache pressure or latency.

The [router source audit](router/README.md) finds that cache events and routing predictions use the actual warmup tokens. Prefix matching stops at a divergent block. The malformed suffix therefore earns no overlap credit for the intended follow-up in the inspected path. Whether it occupies space that other agents need remains unmeasured.

The [retained-block calculation](kv-footprint/README.md) deduplicates the parallel warmup branch by complete block and parent prefix. Assuming completed warmups and no eviction, that branch adds 60.7–64.7% of each recorded session's final real-context KV size, with a 62.1% median. Warmups in the five hint-off sessions are hypothetical. The constructed 50K historical-tool case adds 6.8752 GiB, or 99.33%. These figures describe per-session retained input content; they do not measure physical GPU allocation or concurrent occupancy.

## Related work checked

Tool-argument normalization and speculative-prefix parity are already discussed in [#12109](https://github.com/ai-dynamo/dynamo/pull/12109) and [#12204](https://github.com/ai-dynamo/dynamo/pull/12204). This reproduction isolates missing tool definitions, the new assistant tool call, and the future-role template boundary. The [search record](known-issues.md) includes statuses, current-source inspection, and overlap with other fixes. This is additional evidence in an existing problem area; it makes no novelty claim.

## Local fix

A [local patch](fix/README.md) against Dynamo `f5d3353e2167bb0f0d729085eb5bc9183bf4b222` adds an explicit renderer capability for the pinned Qwen3 tool-continuation template. It preserves the effective request and accumulates the completed assistant, including reasoning and raw tool-argument fragments. It renders the assistant in its tool-history position and ends preparation at the verified closing special token before any unknown tool result.

Unknown templates, text continuations, changed tokenizer artifacts and unsupported identities or settings skip preparation. The patch retains current cancellation and admission controls and leaves client response chunks unchanged. It spans Dynamo and the separately published renderer crate; the [support contract](proposal.md) lists the restrictions and remaining checks.

The native CPU renderer produces 54/54 exact prefixes: 42 recorded tool continuations and 12 constructed length controls. Ordinary request tokens remain unchanged. Renderer and accumulator regression tests pass. The patched `dynamo-llm` test targets compile, and all 19 warmup-module tests pass on macOS ARM64. The generated-tool GPU smoke then verified a 220-token fixed warmup as an exact prefix of its 243-token continuation. Stock warmed 65 tokens and diverged at token 18 in the same smoke fixture.

## GPU comparison

The [September 29 study](../combined-gpu-20260929/PREFILL.md) completed all 18 planned cells on one A100: two saved sessions, off/stock/fixed, three interleaved repetitions. It used matched main frontends at `f5d3353e2167bb0f0d729085eb5bc9183bf4b222`, the pinned vLLM 0.28.0 backend and the Qwen model revision above. Actual generated-tool smokes checked both hint paths before the replay.

| Session | Condition | Whole-session scheduled prefill tokens | Real follow-up cached tokens | Final warmup-only full blocks |
| --- | --- | ---: | ---: | ---: |
| Short | Off | 5,205 | 9,536 | 0 |
| Short | Stock | 8,446 | 9,536 | 200 |
| Short | Fixed | 5,240 | 9,904 | 0 |
| Long | Off | 20,580 | 35,264 | 0 |
| Long | Stock | 38,114 | 35,264 | 1,095 |
| Long | Fixed | 20,595 | 35,328 | 0 |

Every count repeated exactly across all three repetitions. Stock added 62.27% and 85.20% scheduled prefill relative to off, with no additional cache hits on real requests. The fix reduced that work relative to stock by 37.96% and 45.96% and removed the separate branch. Its whole-session prefill remained 35 and 15 tokens above off.

The stock branch reused its own earlier warmups. In the long session, its first warmup computed 17,452 tokens and the second computed 82. The measured cost therefore differs from recomputing the whole context on every tool call.

Scheduled work is the observed change in `vllm:prompt_tokens_by_source_total{source="local_compute"}`, with stable full metric labels and process identity, idle endpoints and exactly one finished request. Captured backend token arrays match every replay input. Gap-free KV event slices have verified opening and closing reset barriers. The stopped PCAP records zero dropped packets.

At the end, the separate stock branch held 200 and 1,095 unique full-block entries. With the verified model and engine geometry, these represent 450 MiB and 2,463.75 MiB of KV payload: 62.31% and 85.28% of the corresponding real contexts' full-block payload. [Per-repetition timings, raw evidence and one-command reproduction](../combined-gpu-20260929/PREFILL.md) are included.

The earlier epoch passed generated-tool smokes. Our measurement identity endpoint then returned HTTP 503 because the backend launcher had been reparented to PID 1. No replay model request was sent and Dynamo did not return a model-request 503. It completed zero measured trials and remains in the [separate attempt record](../combined-gpu-20260929/PREFILL.md#live-path-check-and-earlier-attempt).

## Limits and review status

The comparison is a controlled builder replay. Each request generates one discarded token, so the original assistant's decode cache is absent. Warmups finish before the next request. Timings include tool waits and observation overhead. The fixed long replay still took longer than off despite faster real follow-ups.

KV payload is derived from published full blocks and the selected engine's BF16 geometry. The pool was preallocated. Exact physical allocations, partial blocks, allocator overhead, eviction pressure, live-agent quality, concurrent serving, multiple workers and offload tiers remain unmeasured. The tested runtime fix supports the pinned Qwen tool template; the broader model-family results are CPU rendering checks.

The existing-issue search must be repeated on the filing day. This draft and patch remain local pending review.
