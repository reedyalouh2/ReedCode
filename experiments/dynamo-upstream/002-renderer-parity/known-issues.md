# Existing argument-format work

Checked September 28, 2026. No issue or comment was posted.

**The broad problem is already known.** Tool templates differ in whether historical function arguments should arrive as JSON strings or parsed objects. [Dynamo #12109](https://github.com/ai-dynamo/dynamo/pull/12109) and [#12204](https://github.com/ai-dynamo/dynamo/pull/12204) both address that boundary and speculative rendering. Both were open at inspection.

A [human review of #12332](https://github.com/ai-dynamo/dynamo/pull/12332#discussion_r3692581461) reports that shared argument normalization broke native DeepSeek V3.2 rendering. That PR merged on August 11, 2026. Its final change selects normalization in the preprocessor. The [search record for item 1](../001-speculative-prefill/known-issues.md) preserves the final diff and distinguishes it from earlier discussion.

Our four failures are in DeepSeek-R1's official Jinja template through the published `dynamo-renderer=5.1.0` crate. They use string concatenation and fail after the renderer converts arguments into maps. This adds a concrete regression fixture in the same problem area. The V3.2 native-renderer report is a different path; it cannot establish whether this R1 path is fixed.

## Version check

[Dynamo #14595](https://github.com/ai-dynamo/dynamo/pull/14595) merged September 10, 2026 and upgraded the main branch from renderer 5.1.0 to 5.1.2. Its stated changes concern strict system-message ordering and DeepSeek V4 reasoning effort. The [1.5.1 backport #14806](https://github.com/ai-dynamo/dynamo/pull/14806) was still open at inspection. Neither description reports an R1 argument-concatenation fix.

The [5.1.2 build attempt](renderer-5.1.2-build-attempt/build-attempt.json) resolved renderer 5.1.2, protocols 5.4.1 and Dynamo tokenizers 1.8.1. Compilation stopped in the tokenizer crate at `str::floor_char_boundary`, which is unavailable on the installed Rust 1.90.0 toolchain. No newer binary or fixture result was produced. The manifest, lockfile, wrapper source and their hashes are retained alongside the error.

**Current-runtime behavior remains unverified.** We have also not run this fixture through a full Dynamo frontend. A 5.1.0 failure alone is insufficient to call this a current Dynamo defect.

## Search coverage

The bounded search included both `ai-dynamo/dynamo` and the renderer's own repository, `ai-dynamo/frontend-crates`. The [compact search record](known-issues.json) gives query counts, returned issue identities, and selected primary-source snapshots; its hash is in [evidence-index.json](evidence-index.json). The renderer repository's `arguments` query returned all 68 results; its `DeepSeek R1` query returned all five. The exact R1 Jinja failure did not appear in the inspected results. Two broad Dynamo searches exceeded one page and remain partial.

The local [draft](issue-draft.md) is a regression-fixture note. Before filing it, rerun the case against the current renderer and check the selected full-server path. If an existing thread covers that path, add the fixture there after review. There is no claim of a new general normalization problem.
