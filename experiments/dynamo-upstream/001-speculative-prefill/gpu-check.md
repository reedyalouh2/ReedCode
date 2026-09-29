# Next GPU check

The [combined GPU plan](../combined-gpu-plan.md) is the current proposal. It covers stock-vs-fixed `speculative_prefill` and parity Run 1 on one pod, with a **$5 total cap** including setup, storage and teardown. It supersedes the earlier standalone $2 plan. No rental is authorized.

The [local Qwen3 tool-continuation fix](fix/README.md) has CPU prefix evidence, a passing Dynamo test-target compile, and 19 passing warmup-module tests. Spending and local teardown are approved. Before renting, finish matched stock/fixed Linux artifacts, verify their backend compatibility and freeze the replay. The replacement Runpod key stays in local storage; the user confirmed revocation of the earlier key.

The main comparison measures actual scheduled prefill, follow-up cached tokens and resident warmup-only blocks. It separates the first cold warmup from later warmups that can reuse the same branch. The CPU [cost](session-cost/README.md) and [KV footprint](kv-footprint/README.md) reports supply expectations to check against server evidence.

[Parity Run 1](../parity-run-1/README.md) uses real Claude Code and Codex sessions on stock 1.5.0 with thinking enabled. The plan reserves 25 minutes per harness and two user follow-ups each. The contention check remains queued for a separate proposal.

The combined plan contains the hardware, repetitions, cache resets, tokenizer settings, measurements, stop conditions and evidence requirements. Return the issue, patch and results for review before filing or pushing anything.
