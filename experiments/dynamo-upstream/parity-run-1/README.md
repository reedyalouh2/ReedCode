# Parity Run 1: real coding sessions

**Status: completed.** The [September 29 results](../combined-gpu-20260929/PARITY.md) include Codex at 32K, the incomplete Claude 32K attempt, and the Claude 64K YaRN retry. Both completed sessions matched their compatible-prefix reference. The protocol below preserves the planned 32K setup; configuration changes and clean-start evidence are recorded with the results.

The question is how much reusable prefix history Dynamo loses while serving Claude Code and Codex, and where it is lost. The two sessions use stock Dynamo. The speculative-prefill patch is tested separately.

## Server and clients

Use the recorded Dynamo 1.5.0 image:

```text
nvcr.io/nvidia/ai-dynamo/vllm-runtime@sha256:d18389c89eb319401fdd73f1fbbaff10d9382f634b879941dfba75bbf7260c1e
```

The [deployment record](../../dynamo-20260928/deployment.json) reports vLLM 0.28.0 and embedded Dynamo revision `32b8b2f8c63fa3531c34b64c1cf2cbe39a6f9653`. Keep the image digest authoritative and verify the installed versions before running. The release tag points to a different commit; source checks must identify which revision they inspect.

The first rental could not capture backend traffic under the image's default user. The run used a [runtime-user-only derivative](../gpu-readiness/runpod-retry-proposal/README.md) for the replacement run. Its filesystem layers are identical to the image above; the config changes `User` to `0`. Record both digests and the effective runtime user in the results. The stock 1.5.0 frontend and backend binaries remain unchanged.

Serve `Qwen/Qwen3-8B` at `b968826d9c46dd6066d109eabc6255188de91218`, with BF16 weights and KV, TP=1, prefix caching, 16-token blocks and a 32,768-token context. Set `--dyn-default-thinking-mode enabled`, with the Qwen3 reasoning parser and Hermes tool parser. Inspect the effective prompt and returned reasoning to confirm thinking is active. A client override that turns it off fails readiness.

I chose Qwen3-8B because its `<think>` reasoning and structured tool calls fit on one A100. It is a practical proxy for the mechanisms of interest in GLM and MiniMax. Findings here apply to this Qwen deployment; those other families have not been measured.

| Client | Observed local version | Endpoint |
| --- | --- | --- |
| Claude Code | 2.1.81 | `/v1/messages` |
| Codex CLI | 0.155.1 | `/v1/responses` |

Freeze the actual executable hashes and effective configuration before the run. The documented configuration is in [clients.md](clients.md). Before collection, the frontend was configured with NVIDIA's recommended `--enable-anthropic-api --strip-anthropic-preamble --enable-streaming-tool-dispatch`; all three were available and active. Preserve normal client cache fields and tool schemas, and send no `nvext` fields or agent hints. No request-rewriting adapter, template replacement or serving patch is allowed in this study.

## Workload

Both clients start from independent clean copies of public [ReedCode commit d72cf6df](https://github.com/reedyalouh2/ReedCode/commit/d72cf6dfba2660e1571ed1aa3a0d798e7ee67b31). The task adds a tail-only tool-output retention policy. The two follow-ups add boundary tests and refine where the truncation notice appears. The exact three user messages, file hashes and focused test command are frozen in [task.json](task.json). The five existing focused tests pass on that commit.

Use the real client to read, edit and test the repository. Resume the same client session for each follow-up; never synthesize assistant history. Send follow-ups within ten seconds of the previous turn ending, using prepared messages. Record the actual delay and all model calls. Short pauses reduce an idle-time confound; they cannot prove that eviction never happened.

The capture controller admits at most **15 inference requests per session**, counting retries and ancillary inference. A separate watchdog stops the session at **25 minutes**. It forwards original body bytes and observes responses without changing them. Keep a call ledger for auxiliary traffic instead of silently folding it into the main conversation's prefix history.

Reserve room for both follow-ups. If the first turn has not ended after seven model calls, interrupt at the next completed response boundary using the client's normal control and submit the first follow-up. Use the same rule after four more calls for the second follow-up. Log interruptions and cancellations. Never edit a transcript to make a cancelled response look complete. If the caps prevent two follow-up user messages and their responses, label the session incomplete; it cannot support a parity pass.

Run the clients sequentially. Reset the backend cache and router index before each session, including after smoke tests and between deployment changes. Confirm the first request is cold. Keep cache state between requests inside a session. There is no background load. Task success is recorded but does not decide whether a trace is usable.

## Readiness and capture

Before renting, check each pinned CLI against a CPU-only protocol stub and confirm that it sends the expected route, model, tools and thinking settings. This checks configuration and stream handling. During the GPU readiness window, send **one real smoke request from each harness**, with its normal tool schema, and confirm a successful streamed response. Keep those requests outside the measured sessions and clear their cache. If either fails, save the failure and stop the combined run within the readiness deadline.

The frontend logs `Pre-processed request` at trace level, including full token IDs. The CPU check found that Python worker ingress logs an opaque wrapper. Passive TCP capture is being validated for the backend boundary, alongside JSON traces and KV events. This remains a pre-rental gate. [Source and configuration checks](clients.md#stock-capture-path).

For each model request save:

- Raw incoming body bytes, route, safe headers, client session/user-turn ID and timestamps. Keep auth headers out of the evidence.
- Complete input token IDs received by the backend, normalized request data and matching request/token fingerprints. Frontend-only reconstruction is insufficient for a boundary-capture claim.
- Full streamed response, final usage, stop reason, errors, cancellation and client tool results. Capture raw generated IDs when available; decoded response text alone may not round-trip to the same IDs.
- KV store/remove events with block ancestry, event sequence, worker identity and process epoch. Save startup/cache-reset evidence and gaps in the event stream.
- Submitted, first-token and completion times. Keep instrumentation constant; this run does not estimate uninstrumented latency.

Prove the input-ID capture, request linkage and usage normalization before accepting a session. If stock logging cannot provide them, record an observability blocker. Do not replace missing IDs with an offline rendering and call it observed evidence.

## Metrics

For request `i`, let `P_i` be its captured input IDs, `N_i = len(P_i)`, and `C_i` its final server-reported cached tokens. Only earlier completed requests in the same conversation and worker epoch enter the input-prefix reference.

```text
I_i = 16 * floor(max_j LCP(P_i, P_j) / 16), j < i
actual reuse = C_i / N_i
ideal reuse  = I_i / N_i
gap          = (I_i - C_i) / N_i
```

An empty reference set gives `I_i = 0`. This preserves the requested input-only ideal. Also show an eligible ideal `E_i`, capped by the pinned engine's last-token recomputation rule. Verify that rule in the deployed vLLM before calculating it. Save both values; the final block's eligibility loss is a separate `other: engine boundary` entry.

Normalize usage at the protocol boundary. OpenAI's prompt/input total includes cached tokens. Anthropic separates uncached input from cache reads; reconstruct its total from the final fields, including cache writes if present, and cross-check it against `N_i`. Initial streaming estimates cannot substitute for final usage. Missing usage is unknown, never zero.

Keep negative gaps. Earlier generated tokens can be cached even though they were absent from every earlier input, making `C_i > I_i` possible. Where token/event evidence permits, show a second reference that includes earlier computed decode history. Never silently clamp actual reuse to ideal or include unrelated session cache in the reference.

Per session and request group report `sum(C_i) / sum(I_i)` as requested, alongside `sum(C_i) / sum(N_i)` and `sum(I_i) / sum(N_i)`. Use token sums rather than averaging request percentages. A zero denominator is `N/A`. Show initial cold, within-user-turn and first-request-after-follow-up groups separately. Keep both clients' results visible; two sessions do not establish a population average.

## Cause accounting

The input-only ideal already shrinks when history is rewritten. A small `I_i - C_i` can therefore coexist with a large reasoning or rendering loss. Use two linked ledgers so that the requested cache-gap metric does not hide that mechanism.

**Compatible-prefix misses.** Partition the positive eligible miss interval into nonoverlapping token/block ranges. An early missing block can prevent reuse of the rest of the prefix. Assign its blocked suffix one primary cause. Confirm `eviction` only when the matching chain was stored, then removed, and absent at lookup; a short wait alone is insufficient. Explain boundary rules, invalidation, missing instrumentation or unresolved provenance under `other`. Preserve signed surpluses in a separate column.

**History rewrites.** Compare the response delivered to the client, the next body the client sends, the frontend's normalized history and the official pinned template's output. Locate the earliest proven divergence before newly added content. Record its affected historical suffix length and one primary cause:

| Cause | Evidence required |
| --- | --- |
| Rendering | Same normalized fields and settings produce different official and Dynamo token sequences |
| Reasoning round trip | Reasoning is removed or changed between server output, client echo, normalization or template rendering |
| Tool-call round trip | Tool IDs, names, argument representation or tool-result history change across those boundaries |
| Tool or schema ordering | An order change actually changes historical prefix tokens |
| Harness-side edits | Compaction, rewritten instructions/history, or other client changes beyond the categories above |
| Eviction | Matching compatible block ancestry was stored and then demonstrably removed |
| Other | A concrete different mechanism, interacting causes, or an explicitly unresolved attribution |

Use the first demonstrated causal break; later changes are secondary tags. Where several changes jointly explain it, use `other: interacting causes` instead of choosing an arbitrary winner. Every counted range has exactly one primary label. Historical-suffix counts are diagnostic exposure counts; they are not automatically recoverable tokens, and they must not be added to compatible-miss counts. Each ledger has its own cause ranking.

## Deliverable and stopping rule

Produce a row for every request, a table for each session split by user-turn boundary, both cause rankings, and a one-paragraph verdict. Link each attributed range to raw request IDs, event IDs and token offsets. Report errors, budget stops, incomplete sessions and unattributed counts beside the successful requests.

For this pilot, interpret “a few points” as **three percentage points** in the token-weighted eligible-ideal gap for both clients, separately within-turn and cross-turn. Require two observed follow-up boundaries per client, valid usage/linkage, explained negative gaps and a nonzero reference. Show the ideal's level so a collapse toward zero cannot masquerade as good retention.

If both complete sessions meet that rule, report **“no significant gap observed relative to the compatible-prefix ideal”** and stop further parity GPU work, as requested. This is a descriptive stopping rule; two sessions cannot establish statistical equivalence. Still report any history-rewrite finding separately. If the reference itself collapses, or attribution is missing, say the overall retention question remains unresolved rather than claiming broad parity.

The hosted **85–97%** range is background context only. It has different models, cache policies and measurement definitions and is excluded from all comparisons, denominators and pass criteria. No hosted comparison or cross-model result is claimed here.
