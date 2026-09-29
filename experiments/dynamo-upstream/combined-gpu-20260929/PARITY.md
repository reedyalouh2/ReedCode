# Stock Dynamo with real coding sessions

Both completed sessions reused every compatible full prefix block. The input-only reference understates Codex's reuse because the engine also retained generated tokens. Cross-turn history changes were visible in both harnesses.

## Reuse

Each completed session has 15 admitted HTTP 200 requests, split 7/4/4 across three user turns. Actual and ideal percentages below use summed token counts. The ideal is the longest 16-token-aligned prefix shared with an earlier completed input in the same session and worker epoch.

| Session / group | Requests | Input tokens | Cached tokens | Input-only ideal tokens | Actual reuse | Ideal reuse | Actual / ideal |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Codex 32K, all | 15 | 214,876 | 189,920 | 184,656 | 88.39% | 85.94% | 102.85% |
| Codex 32K, within turn | 12 | 176,738 | 167,840 | 162,576 | 94.97% | 91.99% | 103.24% |
| Codex 32K, cross turn | 2 | 29,512 | 22,080 | 22,080 | 74.82% | 74.82% | 100.00% |
| Claude 64K YaRN, all | 15 | 393,697 | 355,936 | 355,936 | 90.41% | 90.41% | 100.00% |
| Claude 64K YaRN, within turn | 12 | 318,043 | 309,600 | 309,600 | 97.35% | 97.35% | 100.00% |
| Claude 64K YaRN, cross turn | 2 | 55,648 | 46,336 | 46,336 | 83.27% | 83.27% | 100.00% |

The initial cold request is included in each whole-session row. Its cached count was explicitly zero: 0/8,626 for Codex and 0/20,006 for Claude YaRN. Codex's 5,264 extra cached tokens are fully accounted for by earlier captured generation. Recomputing the longest prefix against earlier input plus generated IDs matches actual cached counts on all 30 requests.

Per-request values, response hashes and server errors are in the [Codex report](parity/results/codex-32k/compact-report.json), [Claude YaRN report](parity/results/claude-yarn-64k/compact-report.json), and [original Claude report](parity/results/claude-32k/compact-report.json). The reproduction also produces the full HTTP-to-runtime joins, token arrays, wire decode and capture accounting.

## Why history stopped matching

There were zero positive compatible-prefix misses, so every cause bucket in that ledger is zero: rendering, reasoning round trip, tool-call round trip, schema ordering, harness edits, eviction and other. The signed input-only gap is −5,264 tokens for Codex and zero for Claude YaRN.

A separate history ledger records the old input suffix after the first cross-turn divergence. Each boundary has one primary cause:

| Session | User turn | First differing token, zero-based | Old input suffix exposed | Primary cause |
| --- | ---: | ---: | ---: | --- |
| Codex | 2 | 8,625 | 6,381 | Reasoning round trip through Qwen's template |
| Codex | 3 | 13,465 | 3,519 | Reasoning round trip through Qwen's template |
| Claude YaRN | 2 | 19,485 | 7,394 | Harness-side reminder removal |
| Claude YaRN | 3 | 26,872 | 1,206 | Harness-side reminder removal |

By exposed old-history tokens, reasoning round trip accounts for 9,900 in Codex and harness edits account for 8,600 in Claude. These totals describe different sessions and configurations.

Codex resent its prior Responses input unchanged, including six then ten reasoning items. The pinned Qwen template omits earlier-turn reasoning after a new user message. The observed token windows change from the thinking prefix to the tool-call content. This matches the [saved model template](parity/pinned-chat-template.jinja). [Codex boundary evidence](parity/results/codex-32k/history-rewrite-diagnostic.json).

Claude's raw request already changed before rendering. At the first resume, message 0 lost a 1,516-character Skill-tool reminder. At the second, message 16 lost the corresponding 1,515-character reminder. Later normalization also occurred, but these earliest edits determine the reusable prefix. [Claude boundary evidence](parity/results/claude-yarn-64k/history-rewrite-diagnostic.json).

## Original Claude failure and rerun

At 32K, the fifth Claude request had 26,499 input tokens and requested 8,192 output tokens. Dynamo rejected it. Both scheduled follow-ups also exceeded the context allowance. All seven admitted requests remain in the record: four HTTP 200s and three HTTP 400s. The four usable requests had 63,744 cached and ideal tokens out of 87,854 input tokens; no usable cross-turn request exists. The first continuation attempt followed a 362.99-second diagnostic pause. This incomplete session cannot establish parity.

After prefill finished, 68.16 minutes remained before the admission deadline. The approved retry raised the context limit to 65,536 with YaRN factor 2 and original context 32,768. It retained thinking and Claude's original output budget. That session completed all 15 requests in 320.76 seconds; Codex completed in 169.79 seconds. Their controller gaps between user turns were below 0.01 seconds. The 7/4/4 request caps interrupted unfinished turns as specified. Completing capture does not imply task success.

## Configuration and evidence

Parity used stock Dynamo 1.5.0, vLLM 0.28.0 and the pinned Qwen3-8B revision. Qwen's `<think>` reasoning and structured tool calls made it a practical single-A100 proxy for the mechanisms of interest in GLM and MiniMax. Those families were not measured. Claude Code 2.1.81 used `/v1/messages`; Codex CLI 0.155.1 used `/v1/responses`, on separate copies of [the same frozen public task](../parity-run-1/task.json).

All three measured sessions had `--enable-anthropic-api`, `--strip-anthropic-preamble` and `--enable-streaming-tool-dispatch`. Workers used `--dyn-tool-call-parser hermes`, `--dyn-reasoning-parser qwen3` and `--dyn-default-thinking-mode enabled`. No `nvext` hints or serving patches were used. Exact manifests and the approved clean-start substitution are linked in the [combined report](README.md).

The final Codex and Claude YaRN PCAPs contain 25,504 and 44,630 packets respectively, with zero capture drops. Joins account for reset canaries, smoke calls and control RPCs separately. Claude YaRN's three extra attempts at the proxy's request caps had no forwarded body; they remain recorded as controller-only attempts.

## Verdict and limits

**No significant compatible-prefix gap** appeared in the two completed sessions, so this parity track stops. The historical prefix still shrank across user turns for identifiable reasons. The old suffix counts do not measure avoidable prefill: changing a template or retaining reminders would require a separate quality and performance test. This run provides no evidence of shared-server degradation, eviction pressure or a new Dynamo rendering defect. Hosted 85–97% reuse remains background context; its workload and cache lifetime are different.
