# GPU validation

The [September 29 comparison](../combined-gpu-20260929/PREFILL.md) completed 18 trials on one A100: two sessions, three conditions and three repetitions. It measured actual scheduled prefill, follow-up cache hits and retained warmup-only blocks. The first warmup's cold branch and later warmups' reuse remain separate in the report.

The fix reduced prefill by 37.96% and 45.96% versus stock and removed the parallel branch. [Patch and tests](fix/README.md).

The same study captured real Claude Code and Codex sessions on stock 1.5.0 with thinking enabled. Both completed sessions reused every compatible full prefix block. [Parity report](../combined-gpu-20260929/PARITY.md).

The [method](../combined-gpu-plan.md) records the hardware, repetitions, cache resets, tokenizer settings and measurement definitions. Concurrent serving is the next performance question.
