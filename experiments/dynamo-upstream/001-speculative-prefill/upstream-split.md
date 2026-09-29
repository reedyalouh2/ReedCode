# Upstream patch split

The tested prototype fixes two separate problems: lost rendering inputs and the tool-continuation boundary. Submit them as separate changes. The existing [Dynamo patch](fix/dynamo.patch) and [renderer patch](fix/renderer.patch) remain the exact pair used for the GPU result.

## 1. Preserve the request and complete assistant

Keep this change in Dynamo. Carry the effective request into preparation, including tools, template settings and reasoning options. Accumulate text, reasoning, tool IDs, names and raw argument fragments into the completed assistant message. Forward client chunks unchanged. Preserve the existing cancellation, admission and cleanup controls.

The [cross-model ablation](scope/README.md) isolates the value of this change:

| Tool fixture | Stock matching tokens | Fields restored | Prepared tokens | Exact prefix? |
| --- | ---: | ---: | ---: | --- |
| Qwen3-8B, empty reasoning | 7 | 157 | 182 | No |
| Nemotron-3-Nano | 7 | 306 | 306 | Yes |
| GPT-OSS-20B | 59 | 150 | 150 | Yes |

The first PR should claim that it preserves rendering inputs. These fixtures establish complete tool prefixes for Nemotron and GPT-OSS. Qwen's remaining mismatch belongs in the follow-up.

Extract the request delegation and accumulator from the prototype. Leave template checksums, special-token checks and the renderer trait extension out of this PR. The current `CompletedToolTurn` deliberately accepts only completed tool turns; extraction must make text-turn behavior explicit rather than silently dropping the existing text path.

Required regression cases:

- Tools and effective template settings survive the background handoff.
- Parallel and fragmented tool calls retain their IDs, names and original argument strings.
- Reasoning and assistant content survive independently; usage-only frames do not trigger duplicate preparation.
- Truncated, failed or conflicting streams do not produce a fabricated complete assistant.
- Ordinary response bytes, text-turn behavior, cancellation and admission remain covered by the existing tests.

Build and run this PR independently against the current base. The prototype's passing tests do not substitute for testing the extracted change.

## 2. Render the actual continuation prefix

Add an optional continuation-prefix hook in `dynamo-renderer`, with Qwen tool continuation as its first implementation. Keep the boundary logic next to the template renderer. The Dynamo caller should use the hook and skip preparation when it cannot establish a supported prefix.

The existing implementation renders the completed assistant in its tool-history position, then ends at its closing special token. It never submits the placeholder tool result to the backend. Its pinned template and tokenizer checks establish the prototype's support boundary. Review whether the renderer's own template capabilities can replace those artifact-specific checks without weakening the prefix guarantee.

Retain the 54 exact-prefix cases and add renderer-level fixtures for empty/nonempty reasoning, multiple user turns, parallel calls and tool text containing delimiters. Keep the full GPU comparison attached to this combined behavior: **37.96% and 45.96% less prefill than stock**, with the separate branch removed. Those measurements do not describe PR 1 alone.

## Existing work

[The September 29 check](recheck-20260929/README.md) confirms that current main still has the messages-only wrapper and text-only accumulation. #12109 and #12204 closed without merging, citing the merged argument-normalization and truncated-tool-recovery changes. Those merged diffs leave `speculative_prefill.rs` untouched.

The review on [#12109](https://github.com/ai-dynamo/dynamo/pull/12109#issuecomment-5073755624) already identifies speculative-rendering parity and asks that template behavior live in the renderer. Link it in the issue and both PRs. The new contribution is the missing-field diagnosis, measured compute/KV cost, and tested correction.

This is the extraction plan. The two independently validated PR patches have not yet been produced.
