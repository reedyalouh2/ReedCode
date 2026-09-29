# Speculative-prefill duplicate check

Checked September 28, 2026. This is a read-only source and issue-history audit. No upstream issue, comment, or pull request was submitted.

## Finding

The missing tool definitions, missing newly generated assistant tool calls, and next-role rendering mismatch appear to be distinct reportable causes. They remain in the current `main` source. I found no existing report or proposed fix for those exact causes in this bounded search.

The broader problem of speculative prefill rendering a different prompt from the real request is already known. A human review of [#12109](https://github.com/ai-dynamo/dynamo/pull/12109#issuecomment-5073755624) identifies a tool-argument normalization mismatch. [#12204](https://github.com/ai-dynamo/dynamo/pull/12204) proposes a fix for that cause. Both pull requests are open. A new report should link them and explain the separate missing inputs.

Unnecessary warming is a performance hypothesis. The source can dispatch a warmup without checking whether it adds useful cached blocks, but that alone does not prove a slowdown or wasted GPU prefill. Existing prefix-cache hits can avoid much of the computation.

## Current source

The inspected `main` revision is [`59ac36782c0f9662f1ab6123dce1559d1a3f377a`](https://github.com/ai-dynamo/dynamo/commit/59ac36782c0f9662f1ab6123dce1559d1a3f377a), committed at `2026-09-28T18:57:23Z`.

| Observation | Source | Consequence to test |
| --- | --- | --- |
| `SpeculativePrefillRequest` stores messages only. Its renderer implementation does not forward tools or template arguments. | [Lines 105–136](https://github.com/ai-dynamo/dynamo/blob/59ac36782c0f9662f1ab6123dce1559d1a3f377a/lib/llm/src/preprocessor/speculative_prefill.rs#L105-L136) | Tool-aware or argument-dependent templates can produce a different prefix. |
| The wrapper copies the original messages and sends a string through the warmup channel. | [Lines 171–175](https://github.com/ai-dynamo/dynamo/blob/59ac36782c0f9662f1ab6123dce1559d1a3f377a/lib/llm/src/preprocessor/speculative_prefill.rs#L171-L175) | Other request-level rendering inputs are absent from that handoff. |
| The accumulator reads `choice.delta.content` as text. It does not assemble `tool_calls` or separate reasoning deltas. | [Lines 280–299](https://github.com/ai-dynamo/dynamo/blob/59ac36782c0f9662f1ab6123dce1559d1a3f377a/lib/llm/src/preprocessor/speculative_prefill.rs#L280-L299) | The appended assistant message can differ from the completed response retained by the client. |
| The task appends a text-only assistant message, with other fields defaulted, then renders it as the final message. | [Lines 334–360](https://github.com/ai-dynamo/dynamo/blob/59ac36782c0f9662f1ab6123dce1559d1a3f377a/lib/llm/src/preprocessor/speculative_prefill.rs#L334-L360) | Templates that change earlier assistant serialization when a user or tool message is appended need a separate prefix-parity check. |
| Warmup uses `max_tokens=1`. There is no useful-prefix comparison in this module before dispatch. | [Lines 367–374](https://github.com/ai-dynamo/dynamo/blob/59ac36782c0f9662f1ab6123dce1559d1a3f377a/lib/llm/src/preprocessor/speculative_prefill.rs#L367-L374) | Measure useful additional cache blocks before claiming a benefit or regression. |

The current source includes the lifetime and preprocessing-admission protections from #13438. A fix for rendering must preserve those protections.

The official [v1.5.0 source](https://github.com/ai-dynamo/dynamo/blob/v1.5.0/lib/llm/src/preprocessor/speculative_prefill.rs) has SHA-256 `a8f4bebc8c3db3c7022f8a2a29c1d6f7499b7f944df1e6a91c7f033a3a12d794`. It matches the source preserved from our deployment. It has the same messages-only request and text-only accumulator, and lacks the newer bounded-task implementation. The release was published September 21, 2026; publication date alone does not establish which fixes its branch contains.

The [earlier local reproduction](../../dynamo-prefix/upstream/README.md) supplies the Qwen template examples. This audit does not add a native Rust reproduction or a current-main GPU run.

## Existing reports and changes

Statuses below came from GitHub's issue and pull-request APIs. A closed pull request is described as merged only when `merged_at` is present.

| Report | Status at inspection | Matched cause | Relationship to this report |
| --- | --- | --- | --- |
| [#6230](https://github.com/ai-dynamo/dynamo/pull/6230) | Merged February 15, 2026 | Introduces next-turn speculative prefill. | Original feature. Its review raised a usage-chunk trigger bug; current code triggers on `finish_reason`. |
| [#6502](https://github.com/ai-dynamo/dynamo/pull/6502) | Merged February 23, 2026 | Documents the multi-turn benchmark. | Feature documentation, with no fix for the missing fields. |
| [#13409](https://github.com/ai-dynamo/dynamo/issues/13409) | Closed | Detached warmups can remain alive on stalled backend streams. | Different cause; already addressed upstream. |
| [#13438](https://github.com/ai-dynamo/dynamo/pull/13438) | Merged September 11, 2026 | Owns warmup tasks, bounds their lifetime, links cancellation, and limits blocking preprocessing admission. | Current-main protection to retain. It leaves prefix construction unchanged. |
| [#12109](https://github.com/ai-dynamo/dynamo/pull/12109) | Open, unmerged | GLM-5.2 argument normalization differs between normal and speculative rendering. | Direct prior report of the broader parity problem. Its speculative diff adds an argument-normalization mode. It does not preserve tool schemas, assemble the new assistant's tool calls, or account for the next role. |
| [#12204](https://github.com/ai-dynamo/dynamo/pull/12204) | Open, unmerged | Also propagates tool-argument normalization into speculative rendering. | Same overlap as #12109; inspect before proposing changes to this file. |
| [#12332](https://github.com/ai-dynamo/dynamo/pull/12332) | Merged August 11, 2026 | Normalizes historical tool-call arguments for the normal preprocessor's selected template path. | The PR body and early bot summaries describe wider speculative coverage than its final diff. The merged diff does **not** change `speculative_prefill.rs`; do not cite it as fixing our causes. |
| [#9946](https://github.com/ai-dynamo/dynamo/pull/9946) | Merged June 1, 2026 | Moves rendering into `dynamo-renderer`. | Refactor; the messages-only speculative request remains. |
| [#7768](https://github.com/ai-dynamo/dynamo/pull/7768) | Merged April 8, 2026 | Tool-call loss while parsing speculative-decoding output. | Similar words, separate mechanism. It does not change speculative-prefill prefix construction. |
| [#10659](https://github.com/ai-dynamo/dynamo/pull/10659) | Merged June 12, 2026 | Updates the agent-hint documentation. | Documents the feature; no implementation fix. |
| [#10662](https://github.com/ai-dynamo/dynamo/pull/10662) | Closed, unmerged | Related agent-hint documentation changes. | Do not describe this PR as a merged fix. |

Bot comments were used to locate code and earlier discussion. Source, final diffs, merge metadata, and the human review establish the conclusions above. In particular, the stale #12332 summaries would lead to the wrong duplicate verdict.

## What the documentation promises

The [v1.5.0 agent-hints table](https://github.com/ai-dynamo/dynamo/blob/v1.5.0/docs/fern/pages/use-cases/agents/agent-hints.md#L34) describes warming the predicted next-turn prefix after the current turn completes. The [v1.5.0 SGLang guide](https://github.com/ai-dynamo/dynamo/blob/v1.5.0/docs/fern/pages/developer-guide/knowledge-base/modular-components/backends/sglang/agents-on-sglang.md#L97) describes an ordinary `max_tokens=1` request after response completion. Both support testing whether the prepared tokens match the next request.

The [v1.5.0 agentic-inference article](https://github.com/ai-dynamo/dynamo/blob/v1.5.0/docs/fern/pages/blog/2026/agentic-inference-optimizations.mdx#L99) describes warming before a tool call returns. The implementation can run during that pause because it starts after the assistant response finishes. It does not stream tool output into the warmup. The article also includes tools in its request example, making missing tool definitions relevant to the documented agent workload.

These three passages are unchanged in the inspected `main` revision. No benchmark number in those documents proves the proposed fixes improve this workload.

## Search coverage and limits

The search covered open and closed issues and pull requests in `ai-dynamo/dynamo`. Sixteen focused queries returned their complete result sets. Four broader reconnaissance queries returned the first 100 results and are marked partial below. GitHub reported `incomplete_results=false` for every query; that flag does not remove pagination limits.

The broad speculative-prefill query returned 73 issues and pull requests. I retrieved all 1,131 discussion and review comments on those results through 131 API calls. No comment response reached the 100-item page limit. I searched those comments for `speculative_prefill`, `speculative prefill`, `speculative-prefill`, `SpeculativePrefillRequest`, and `prefill_task`, then reviewed the matching discussions. The evidence also includes the seven commits in the source file's returned history and final diffs for the closest overlapping pull requests.

This is a bounded duplicate check. It cannot exclude private tracking, descriptions using unrelated terms, an unindexed change, or later upstream work. The defensible conclusion is that these exact causes were not found in the inspected public results.

| Query | Results returned / total | Coverage |
| --- | ---: | --- |
| ["speculative_prefill"](https://github.com/search?type=issues&q=repo%3Aai-dynamo/dynamo%20%22speculative_prefill%22) | 8 / 8 | Complete |
| ["speculative prefill"](https://github.com/search?type=issues&q=repo%3Aai-dynamo/dynamo%20%22speculative%20prefill%22) | 73 / 73 | Complete |
| ["speculative-prefill"](https://github.com/search?type=issues&q=repo%3Aai-dynamo/dynamo%20%22speculative-prefill%22) | 73 / 73 | Complete |
| [prefill tools](https://github.com/search?type=issues&q=repo%3Aai-dynamo/dynamo%20prefill%20tools) | 100 / 641 | First page only |
| [prefill tool_calls](https://github.com/search?type=issues&q=repo%3Aai-dynamo/dynamo%20prefill%20tool_calls) | 38 / 38 | Complete |
| [prefill reasoning](https://github.com/search?type=issues&q=repo%3Aai-dynamo/dynamo%20prefill%20reasoning) | 100 / 293 | First page only |
| ["cache warm"](https://github.com/search?type=issues&q=repo%3Aai-dynamo/dynamo%20%22cache%20warm%22) | 100 / 106 | First page only |
| ["cache warming"](https://github.com/search?type=issues&q=repo%3Aai-dynamo/dynamo%20%22cache%20warming%22) | 6 / 6 | Complete |
| [prefix template](https://github.com/search?type=issues&q=repo%3Aai-dynamo/dynamo%20prefix%20template) | 100 / 1631 | First page only |
| [speculative redundant](https://github.com/search?type=issues&q=repo%3Aai-dynamo/dynamo%20speculative%20redundant) | 56 / 56 | Complete |
| [prefill chat_template_kwargs](https://github.com/search?type=issues&q=repo%3Aai-dynamo/dynamo%20prefill%20chat_template_kwargs) | 12 / 12 | Complete |
| [prefill "tool schema"](https://github.com/search?type=issues&q=repo%3Aai-dynamo/dynamo%20prefill%20%22tool%20schema%22) | 25 / 25 | Complete |
| ["SpeculativePrefillRequest"](https://github.com/search?type=issues&q=repo%3Aai-dynamo/dynamo%20%22SpeculativePrefillRequest%22) | 5 / 5 | Complete |
| ["prefill_task"](https://github.com/search?type=issues&q=repo%3Aai-dynamo/dynamo%20%22prefill_task%22) | 5 / 5 | Complete |
| ["speculative prefill" tools](https://github.com/search?type=issues&q=repo%3Aai-dynamo/dynamo%20%22speculative%20prefill%22%20tools) | 25 / 25 | Complete |
| ["speculative prefill" template](https://github.com/search?type=issues&q=repo%3Aai-dynamo/dynamo%20%22speculative%20prefill%22%20template) | 38 / 38 | Complete |
| ["speculative prefill" reasoning](https://github.com/search?type=issues&q=repo%3Aai-dynamo/dynamo%20%22speculative%20prefill%22%20reasoning) | 26 / 26 | Complete |
| ["speculative prefill" redundant](https://github.com/search?type=issues&q=repo%3Aai-dynamo/dynamo%20%22speculative%20prefill%22%20redundant) | 6 / 6 | Complete |
| ["speculative prefill" duplicate](https://github.com/search?type=issues&q=repo%3Aai-dynamo/dynamo%20%22speculative%20prefill%22%20duplicate) | 24 / 24 | Complete |
| ["speculative prefill" prefix](https://github.com/search?type=issues&q=repo%3Aai-dynamo/dynamo%20%22speculative%20prefill%22%20prefix) | 36 / 36 | Complete |

## Evidence files

`evidence-index.json` records query coverage, the source pin, archive contents, and SHA-256 hashes. `search-evidence.tar.gz` preserves the raw search results, selected issue and review snapshots, filtered comment audit, and source/document snapshots. Each archived JSON has a hash in the index and in the archive's manifest.

The archive is for reproducing this check. The report above is the review entry point.
