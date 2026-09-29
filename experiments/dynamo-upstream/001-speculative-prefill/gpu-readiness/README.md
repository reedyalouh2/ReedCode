# Controlled prefill replay

The CPU packet is ready. It contains one recorded coding session, one longer transcript built from executed repository tools, and the stock/fixed token arrays for every continuation. No GPU requests have been made with these fixtures.

**This is a controlled builder replay.** Each HTTP request generates one discarded token. It then supplies the saved assistant history as part of the next input. The original assistant's decode KV is absent, so the extra reuse credited to a fixed warmup can exceed its benefit in a live agent. Generated-tool checks exercised both actual hint paths before the [completed GPU comparison](../../combined-gpu-20260929/PREFILL.md).

## Frozen sessions

| Session | Source | Tool continuations | Original input tokens | Final follow-up tokens |
| --- | --- | ---: | ---: | ---: |
| Short | Recorded `noisy-bugfix_r01_on` | 4 | 381–3,293 | 5,150 |
| Long | Executed tools reading public ReedCode files and running focused tests | 2 | 17,610–17,680 | 20,554 |

The short session preserves the archived assistant and tool messages. The long session uses public commit `d72cf6dfba2660e1571ed1aa3a0d798e7ee67b31`. Its bash-call messages were authored for the commands in the fixture; Qwen did not generate that session. The source files, executed commands, combined tool output, exit codes and measured CPU tool waits are frozen under [fixtures](fixtures/manifest.json). The focused output-policy suite passed all 5 tests.

Both use `Qwen/Qwen3-8B` revision `b968826d9c46dd6066d109eabc6255188de91218`, with thinking disabled. The native builders use Dynamo main `f5d3353e2167bb0f0d729085eb5bc9183bf4b222` and the reviewed [patches](../fix/README.md). All input lengths plus the one-token output allowance fit 32,768 tokens.

| Continuation | Stock warmup tokens | First differing token, zero-based | Fixed prefix tokens |
| --- | ---: | ---: | ---: |
| Short 1 | 93 | 31 | 398 |
| Short 2 | 2,657 | 31 | 2,962 |
| Short 3 | 2,894 | 31 | 3,269 |
| Short 4 | 3,005 | 31 | 3,310 |
| Long 1 | 17,468 | 24 | 17,639 |
| Long 2 | 17,538 | 24 | 17,704 |

All 6 fixed warmups are exact prefixes of their following request. Ordinary request tokens are unchanged. The saved arrays give the precise boundary for these fixtures; the earlier token-40/token-56 minimal cases have different prompts.

## CPU verification

From the repository root:

```bash
readiness=experiments/dynamo-upstream/001-speculative-prefill/gpu-readiness
uv run python "$readiness/check.py"
```

To rerun both compiled native builders against the frozen inputs:

```bash
uv run python "$readiness/check.py" \
  --stock-binary /tmp/reedcode-current-stock-prefill/main/reproduction-binary \
  --fixed-binary /tmp/reedcode-current-stock-prefill/target/debug/reedcode-prefill-fix-check
```

The check verifies 21 fixture hashes, 15 saved source files, all 6 token comparisons, 27 unit tests, and the dry-run CLI paths. It saves [cpu-validation.json](cpu-validation.json) and [test-readiness.log](test-readiness.log). The SSE and reset tests use mocked responses; they do not open server connections.

The native rerun needs the pinned tokenizer and template at the paths in `fixtures/input.json.gz`. Binary and patch hashes must match the manifest. `prepare.py --output /tmp/prefill-new-fixtures` builds a separate packet from the same recorded trace and public git revision. Tool timing and unittest duration text may change when those commands are executed again; the checked-in packet remains the study input.

## Replay sequence

[schedule.json](fixtures/schedule.json) freezes 18 units: two sessions, three conditions and three repetitions. Conditions rotate `off/stock/fixed`, `stock/fixed/off`, then `fixed/off/stock`.

Within each unit, send the initial real input, the selected warmup, and the following real input. Continue through every tool continuation. `off` skips warmups. Real fixture IDs and payloads stay paired across conditions; HTTP request IDs include the condition and repetition. A warmup must finish before the following request. That follow-up also waits until the saved tool delay has elapsed since the preceding real completion; a slower warmup extends this delay. Request start offsets record the actual schedule.

Preview without opening a connection:

```bash
uv run python "$readiness/replay.py" --session long --condition stock --repeat 1
```

Use the [trial coordinator and report commands](execution.md) for the live study. The coordinator proves each backend and router reset, preserves opening and closing KV event barriers, then runs the selected fixture with raw metrics and worker-identity snapshots.

The lower-level `replay.py` remains available for controlled debugging with an independently verified reset record. It saves each payload, raw SSE, response ID, usage and timing. Missing cached usage, unexpected prompt lengths, incomplete streams or a cached first request fail the attempt. Failed records remain on disk. The study controller and watchdog enforce the rental limit separately from request deadlines.

## Remaining live checks

- Verify that token-input completions reach the backend with the exact saved IDs. Capture the received IDs and compare them before interpreting cache reuse.
- Exercise a generated tool call through each actual hint path. Save the returned assistant, internal warmup and real continuation. This packet only invokes the native builders during CPU preparation.
- Prove cache/index reset and continuous KV-event collection. [reset.md](reset.md) records the pinned source path and the part still requiring live confirmation.
- Join each HTTP request to backend scheduled-prefill accounting, cache events and process identity. SSE usage and these CPU arrays alone cannot show physical KV residency or scheduled GPU work.

The [combined method](../../combined-gpu-plan.md) records the deployment and measurement design.
