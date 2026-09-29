# Dynamo upstream work

The goal is verified Dynamo fixes that help agentic workloads. Findings stay local until review. Single-GPU evidence comes first; claims about multiple workers, routing scale, or offload tiers are out of scope.

| Order | Candidate | Current state |
| --- | --- | --- |
| 1 | Stock `speculative_prefill` | Weakness confirmed. A [local Qwen3 tool-continuation fix](../experiments/dynamo-upstream/001-speculative-prefill/fix/README.md) produces 54/54 exact CPU prefixes with ordinary request tokens unchanged. [Branch-size accounting](../experiments/dynamo-upstream/001-speculative-prefill/kv-footprint/README.md) is complete. [18 GPU replay trials](../experiments/dynamo-upstream/combined-gpu-20260929/PREFILL.md) now show added stock prefill work and a parallel cache branch; the fix removes that branch. Filing-day issue search and review remain open. |
| 2 | Frontend rendering parity | [44 CPU cases through the 5.1.0 Rust formatter](../experiments/dynamo-upstream/002-renderer-parity/README.md): 40 exact matches and four DeepSeek tool-history errors. Argument compatibility is already known upstream. A temporary Rust 1.96.1 toolchain is now available; the cross-model current-version check remains pending. |
| 3 | vLLM `priority` semantics | [Already documented](../experiments/dynamo-upstream/003-priority/README.md): scheduling is supported; priority-based vLLM eviction remains planned. Source check complete; controlled runtime tests remain unrun. |
| 4 | `cache_control` pinning | [Already resolved upstream](../experiments/dynamo-upstream/004-cache-control/README.md): the experimental API was removed in #7790. The 1.5 reference and article agree. |
| 5 | Observability gaps | [Recorded with their measurement impact](../experiments/dynamo-upstream/005-observability/README.md): restart continuity, preparation attribution, block reuse, and lifecycle consumers. Ownership and known limitations are kept separate. |
| After this run | Fixed-RPC-port registration | [Startup failure and source comparison](../experiments/dynamo-upstream/006-fixed-rpc-registration/README.md) saved. The 1.5 frontend failed model registration with a fixed TCP RPC port. Main already changed this initialization path. Existing-issue search and an isolated CPU reproduction remain pending; a new upstream bug is unconfirmed. |

## Definition of done: #1 `speculative_prefill`

Not ready to file until each item is checked:

- [x] **Intended use confirmed.** Dynamo docs and source state what the hint should warm; our pilot used it as intended. [Evidence](../experiments/dynamo-upstream/001-speculative-prefill/current-code/intended-use.md).
- [x] **Still present on current code.** CPU reproduction rerun against the latest release and `main`. [Pinned builds and results](../experiments/dynamo-upstream/001-speculative-prefill/current-code/README.md).
- [x] **Root cause named.** One sentence explaining why the warmup diverges at token 40 (tools) and 56 (text), pointing to the code path. [Causes and source links](../experiments/dynamo-upstream/001-speculative-prefill/current-code/intended-use.md#exact-causes).
- [x] **Scope measured.** Same check on 2–3 more model families. The missing schema affects Qwen, Nemotron and GPT-OSS in the tested cases. Text boundaries vary; DeepSeek tool rendering errors prevent its tool-prefix comparison. [Cases, ablations and pins](../experiments/dynamo-upstream/001-speculative-prefill/scope/README.md).
- [x] **Fix shown to help.** The GPU run completed all 18 cells: two sessions, three conditions, three repetitions. Relative to stock, the fix cut scheduled prefill by 37.96% and 45.96%, increased real follow-up cache hits, and removed the warmup-only branch. [GPU results and reproduction](../experiments/dynamo-upstream/combined-gpu-20260929/PREFILL.md).
- [x] **Existing issues and PRs searched again September 29.** Current main still has the defect; #12109 and #12204 closed without merging. [Updated search and source](../experiments/dynamo-upstream/001-speculative-prefill/recheck-20260929/README.md). Repeat the check if filing on a later date.

The [stock CPU check](../experiments/dynamo-upstream/001-speculative-prefill/current-code/README.md) reproduces token 40 and 56 on v1.5.0 (`b83b1d9`) and `main` (`f5d3353`), checked September 28, 2026. The local patch preserves the request and completed assistant, then uses an explicit Qwen tool-continuation boundary. Other templates and text continuations skip preparation. The patched Dynamo crate compiles; all 19 warmup-module tests pass.

On September 29, the [GPU comparison](../experiments/dynamo-upstream/combined-gpu-20260929/PREFILL.md) found that stock warmups added 62.27% and 85.20% scheduled prefill work with no additional real-request cache hits. Their retained branch represented 450 MiB and 2,463.75 MiB of full-block KV payload, or 62.31% and 85.28% of the final real context. The fix removed that branch. Counts repeated exactly across all three repetitions.

These are controlled builder replays. The timings include serial warmups and observation overhead; physical allocations, shared-server effects and agent quality remain unmeasured. [Limits and per-repetition results](../experiments/dynamo-upstream/combined-gpu-20260929/PREFILL.md#limits).

The separate [stock parity run](../experiments/dynamo-upstream/combined-gpu-20260929/PARITY.md) completed 15 calls and two follow-ups in each harness. Codex at 32K and Claude at 64K YaRN reused every compatible full prefix block. The original Claude 32K failure remains recorded. That compatible-prefix parity track stops with no significant gap; the cross-model rendering audit stays queued.

The [first rental](../experiments/dynamo-upstream/gpu-readiness/gpu-attempt-01/README.md) failed capture permissions and was deleted before model download. Changing the container runtime user enabled packet capture. Its first prefill epoch passed tool smokes, then our measurement identity endpoint returned HTTP 503 before the first replay model request was sent. The backend launcher had been reparented to PID 1, changing the recorded process identity. The [failed epoch remains separate](../experiments/dynamo-upstream/combined-gpu-20260929/PREFILL.md#live-path-check-and-earlier-attempt) from the 18 successful trials.

Next: review the issue and extract the two changes in the [patch split](../experiments/dynamo-upstream/001-speculative-prefill/upstream-split.md). The September 29 search is complete.

## After #1: next milestones

1. **Issue + PR for #1.** Keep the PR small, include a regression test, and respond to review quickly.
2. **#2 rendering and cache compatibility.** This is the next audit after #1, with the speculative-prefill defect as its first regression case. Rust 1.96.1 is available; the current main snapshot uses renderer 5.4.0, superseding the earlier 5.1.2 target. Extend the official-template parity check across multi-turn history, tool calls and reasoning settings. File DeepSeek cases only if the known issues leave them uncovered.
3. **Cache-compatibility test suite (upstream).** Propose the suite in the #1 thread first; build it while #1 is in review. Keep each supported template's rendering and reusable-prefix tests together.
4. **Measured payoff on their tools.** AIPerf replaying TraceLab plus our sessions: time to first token and cached tokens with stock vs. fixed `speculative_prefill`. The controlled prefill and branch measurements are complete. A concurrent study needs a representative workload and a separate experiment.
5. **#5 observability:** file after #1, using it as the concrete example of what the missing linkage blocked.
6. **Write-up:** problem, fix, test suite, and measured gain, offered to the Dynamo team.

For each candidate: reproduce, search existing issues and PRs, draft an issue, and propose a fix. Already-known problems should be recorded with links before moving on. A verified report needs exact versions, commands, raw evidence with hashes, an impact estimate, and clear limits.

The issue and patch are prepared for review. The current patch spans request preservation and a renderer hook; [the split plan](../experiments/dynamo-upstream/001-speculative-prefill/upstream-split.md) separates those changes.

The dead-suffix study and further prefix-preparation research are paused. Existing records remain available as evidence.
