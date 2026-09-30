# Upstream check, September 29

At 09:33 UTC, main was [`51b83df91f3fca2f52ed644f744c4465c3d163b3`](https://github.com/ai-dynamo/dynamo/commit/51b83df91f3fca2f52ed644f744c4465c3d163b3). Its `speculative_prefill.rs` is byte-identical to the module tested at `f5d3353`: SHA-256 `d1ea24631f4d7c9e731615948bc9a128afdd70a4326bad87cf1569200b1535fb`.

The [wrapper still stores only messages](https://github.com/ai-dynamo/dynamo/blob/51b83df91f3fca2f52ed644f744c4465c3d163b3/lib/llm/src/preprocessor/speculative_prefill.rs#L109), and the [accumulator still reads only text deltas](https://github.com/ai-dynamo/dynamo/blob/51b83df91f3fca2f52ed644f744c4465c3d163b3/lib/llm/src/preprocessor/speculative_prefill.rs#L285). The missing-field cause is still present.

## Related PRs changed status

[#12109](https://github.com/ai-dynamo/dynamo/pull/12109#issuecomment-5881677726) and [#12204](https://github.com/ai-dynamo/dynamo/pull/12204#issuecomment-5881676260) closed without merging at 01:07 UTC. Their author cited two earlier merged changes:

- [#12332](https://github.com/ai-dynamo/dynamo/pull/12332), merged August 11: tool-argument normalization.
- [#12333](https://github.com/ai-dynamo/dynamo/pull/12333), merged August 10: truncated tool-call recovery.

The saved final file lists confirm that neither merged change touches `speculative_prefill.rs`. Their closure does not resolve the messages-only warmup builder or its text-only assistant reconstruction. The earlier review remains relevant prior work on speculative-prefix parity.

## Search coverage

Four queries returned complete result sets: `"speculative prefill"` (74), `"speculative_prefill"` (8), `prefill "tool schema"` (25), and `"SpeculativePrefillRequest"` (5), all scoped to `ai-dynamo/dynamo`. There were 96 unique results. Comparing them with the [September 28 search](../known-issues.md) found three changed metadata records: the two PR closures and an update to #13957, whose body was unchanged.

The source and merged diffs establish that the defect remains. The search provides context for filing; it cannot exclude private tracking or differently worded reports.

[manifest.json](manifest.json) records the check time, query counts and hashes of the source and merged diffs.
