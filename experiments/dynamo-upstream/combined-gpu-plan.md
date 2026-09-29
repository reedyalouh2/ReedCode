# Combined GPU check: speculative prefill and parity

**Completed September 29, 2026 UTC.** [Results, exact configurations and reproduction](combined-gpu-20260929/README.md). All 18 prefill trials and both real harness sessions completed, with an approved 64K YaRN retry for Claude. The pod is deleted; conservative total cost is $2.37. The plan and approval history below are retained as written before collection.

Status: the user approved the combined run and the local watchdog/controller cleanup on September 28, 2026, with a **$5** total cap including setup, storage and teardown. The user also confirmed revocation of the earlier key. CPU readiness gates passed. The first rental failed the live capture-permission gate and was deleted before model download or inference. [Attempt record](gpu-readiness/gpu-attempt-01/README.md). This replaces the earlier standalone $2 proposal.

The user then approved one replacement rental using a derived image whose only config change is `User: dynamo` to `User: 0`. All filesystem layers stay identical. The [retry record](gpu-readiness/runpod-retry-proposal/README.md) tracks publication and the remaining CPU image check. Reserve $0.17 for the first allocation within the same $5 total. A CPU-only image-transfer helper has been proposed separately and is awaiting approval; no such pod has been created.

The first experiment checks whether the local speculative-prefill fix removes the incompatible warmup branch and changes follow-up reuse. [Parity Run 1](parity-run-1/README.md) then measures real Claude Code and Codex sessions on the recorded stock 1.5.0 image, with thinking enabled and two user follow-ups per session. The supplied specification is now frozen in that protocol.

These are separate serving configurations on the same GPU:

| Study | Frontend | Thinking | Hints |
| --- | --- | --- | --- |
| Speculative-prefill control | Unpatched main `f5d3353e2167bb0f0d729085eb5bc9183bf4b222` | Disabled | Off, then stock hint |
| Speculative-prefill fix | Same main revision plus the two local patches | Disabled | Fixed hint |
| Parity Run 1 | Recorded stock 1.5.0 image, embedded revision `32b8b2f8c63fa3531c34b64c1cf2cbe39a6f9653` | Enabled | None |

The stock-versus-fix comparison keeps the revision matched. Parity never uses the patched frontend. Restart with the appropriate configuration and clear cache state between studies; never compare a 1.5.0 baseline against a patched main and call the difference a fix effect.

The [CPU patch checks](001-speculative-prefill/fix/README.md) are complete: 54/54 exact prefixes, unchanged normal requests, a passing Dynamo test-target compile and 19 passing warmup-module tests. The [branch-size report](001-speculative-prefill/kv-footprint/README.md) supplies the CPU estimate to compare with actual residency.

## Machine and budget

Use one on-demand A100 80GB pod, preferably the same SXM variant as the September 28 runs. Keep all conditions on that machine. No second pod, persistent volume, paid build service, or automatic replacement.

Runpod's public price on September 28, 2026 is $1.59/hour for A100 PCIe and SXM. The accepted quote must be **at most $1.65/hour**, checked immediately before creation. This is a planning ceiling, not a reservation. [GPU pricing](https://www.runpod.io/pricing).

| Item | Maximum |
| --- | ---: |
| GPU, 150 minutes at $1.65/hour | $4.125 |
| 150GB temporary container disk | $0.10 |
| Teardown and billing margin | $0.775 |
| Total | **$5.00** |

The current disk rate is $0.10/GB/month, which is about $0.052 for 150GB over 150 minutes using a 720-hour month. The budget reserves $0.10. Runpod bills pod compute and disk by the second and reports no ingress or egress fee. [Billing details](https://docs.runpod.io/pods/pricing).

The approved proposal required a provider-side termination deadline at 150 minutes. That prerequisite failed: current runpodctl 2.14.0 has no termination timer, and the backend previously ignored the deadline fields. The restoration PR remains a draft. [Saved control-plane evidence](gpu-readiness/control-plane/README.md).

The approved replacement is a detached local watchdog, kept awake with `caffeinate`, alongside the running study controller. It closes request admission at minute 130 and deletes the pod by minute 140, or at minute 35 if readiness fails. It retries deletion until the pod disappears from the account list. The controller also deletes in its cleanup path and watches both elapsed time and the current quote. The API key stays on the Mac. [Watchdog and CPU checks](gpu-readiness/watch_pod.py).

This substitute cannot enforce the deadline while the Mac is powered off or disconnected from Runpod. The user accepted that limitation when approving the substitute. The $5 cap is unchanged. Copy evidence continuously, start teardown earlier if billing or rate changes consume the margin, and confirm pod deletion and zero active spend. A stopped pod is insufficient.

## Before the meter starts

1. Confirm that the exposed key has been revoked. Read its replacement only from local storage after approval; never place it in a command line, report or transcript.
2. Use the tested patch against Dynamo `f5d3353e2167bb0f0d729085eb5bc9183bf4b222`, whose renderer version is 5.4.0. Freeze the patch, test results and Linux artifacts in a manifest. Stock and fixed must use that same revision and differ only by the patch; include the renderer patch and its revision. The saved macOS test binary does not replace the Linux frontend build.
3. Build and smoke-test the Linux frontend artifacts before renting. Keep the backend, model, launch options and instrumentation identical. The previous image is `nvcr.io/nvidia/ai-dynamo/vllm-runtime@sha256:d18389c89eb319401fdd73f1fbbaff10d9382f634b879941dfba75bbf7260c1e`, containing Dynamo 1.5.0 and vLLM 0.28.0. It is a candidate backend base only after the current-main frontend passes protocol and ABI smoke checks against it. Any new matched image must have its own recorded digest and backend version.
4. Pin `Qwen/Qwen3-8B` at `b968826d9c46dd6066d109eabc6255188de91218`, BF16 weights and KV, tensor parallel size 1, prefix caching enabled, 16-token blocks, thinking disabled for the recorded-session reproduction. Set `DYN_TOKENIZER_BACKEND=default` and `DYN_TOKENIZER_CACHE=0` in both stock and fixed frontends to exercise the validated Hugging Face tokenizer contract. Verify the template/tokenizer checksums and settings at startup. Start with the previous 32,768-token context limit; every request plus its output allowance must fit.
5. Freeze one recorded coding session and one longer session made from real repository files, tool results and assistant messages. The long session must have at least two tool continuations, with most history after the tool schemas. Check its normal, stock and fixed token sequences on CPU. Target 16K–24K input tokens so no context-limit change is needed. Repeated filler controls stay labeled separately. Hash every request, expected continuation and arrival schedule.
6. Prepare the two pinned real clients and the [frozen public task](parity-run-1/task.json). Check custom-endpoint configuration and streaming against a CPU stub. Prepare independent clean task copies, a byte-preserving capture proxy, per-session 15-request/25-minute limits, and both saved follow-up messages. Validate stock logging and response-to-request linkage without adding hints or changing bodies.
7. Resolve the failed provider-timer gate above. If the local substitute is approved, verify both watchdog/controller cleanup paths and their saved allocation start before creation. Freeze launch scripts and the replay/report command. Confirm local storage has room for the results and that the scripts redact credentials.

The CPU patch/tests, public task, [long prefill fixture](001-speculative-prefill/gpu-readiness/README.md) and [stock source audit](parity-run-1/source-check/README.md) are complete. The shipped stock frontend accepts both real clients' captured bodies in the CPU transport check. Matched Linux artifacts, the complete coding-tool client checks and passive backend capture passed their CPU checks. Python worker trace logging was tested and exposes only an opaque wrapper, so it cannot supply the required backend token IDs. Actual deployment smoke tests remain inside the approved GPU readiness window.

## Run order

| Elapsed time | Work |
| --- | --- |
| 0–35 minutes | Load and verify the deployment, token capture, one real smoke request per harness, and fixed-hint dispatch |
| 35–70 minutes | Speculative-prefill comparison |
| 70–75 minutes | Return to stock 1.5.0, enable thinking, verify cache/index reset |
| 75–100 minutes | Claude Code session, at most 15 model calls and 25 minutes |
| 100–105 minutes | Reset cache/index; prepare Codex's independent task copy |
| 105–130 minutes | Codex session, at most 15 model calls and 25 minutes |
| 130–140 minutes | Export, validate hashes, delete pod and verify billing state |
| 140–150 minutes | Termination margin only |

If either harness smoke test, complete token capture or fixed-hint readiness fails, save the failure and delete the pod by minute 35. Do not spend the experiment time on an open-ended build or installation. End the prefill phase at minute 70 even if some repeats remain; preserve incomplete pairs and give both harnesses their reserved time. Export evidence continuously. The 150-minute limit is fixed.

## Speculative-prefill comparison

Compare **hint off**, **stock hint**, and **fixed hint**. Use three repetitions per session and rotate the order across repetitions: off/stock/fixed, stock/fixed/off, fixed/off/stock. Use the same arrival delays and output limits. All instrumentation stays on in every condition. The first patch supports default routing only; leave worker targeting, cache namespaces, LoRA, priority and other routing hints unset in the matched experiment. Record any changes made to archived request metadata. Confirm that a fixed supported tool turn actually dispatches a warmup before measuring its reuse.

Each repetition starts from an empty backend cache and router index. Validate the reset procedure during preflight; prefer a verified cache reset over reloading the model for every condition. Keep the cache between turns within a session. Verify the first request has zero cached tokens and that cache-event tracking starts from a known empty state. A fresh first-block marker can isolate reuse but cannot prove equal cache occupancy, so it cannot replace this reset for footprint measurements.

First verify the end-to-end stock and fixed hint paths with an actual generated tool call. Save the returned assistant, the internal warmup and the real tool-result continuation. Compare the exact request and assistant token sequences across conditions. If generation changes the trajectory, report it and exclude that pair from the fixed-input latency comparison; retain all failures and skip counts.

For the controlled cache comparison, replay the same saved assistant and continuation through a test seam that invokes the stock or fixed request builder. It must use that builder's exact token IDs and the real backend. Label this as a controlled builder replay. It measures the rendering change while the end-to-end check establishes that the hint calls the builder correctly. Keep warmup completion explicit before the follow-up in the primary comparison. An additional replay with recorded tool delays can measure cancellation and overlap if time remains.

A fix may skip an unsafe warmup. Count that outcome. Its benefit would be avoiding the incompatible branch; it should not be described as improved preparation or extra cache reuse.

Record, per request and per warmup:

- Parent request ID, condition, trial, token hashes, completion/cancellation/skip reason, and backend process identity.
- Actual scheduled prefill tokens and cache-hit tokens. Validate their definitions against the pinned backend. A total prompt-token counter includes reused tokens and cannot measure recomputation.
- KV `Stored` and `Removed` events, complete token-chain hashes, and a reconstructed resident-block set. Repeated publication does not mean a second allocation. A gap or restart invalidates resident-size attribution until the cache is reset. Events describe cacheable full blocks; report active partial-block allocations separately if the backend exposes them.
- Warmup-only resident blocks, blocks shared with the real history, and useful real blocks removed. Report branch bytes divided by the contemporaneous real-context bytes. Also report unique retained history so stale real branches are visible. Verify bytes per block from the engine's actual allocation; the CPU BF16 estimate alone cannot establish physical residency.
- Follow-up cached tokens, time to first token, completion latency and total session time. Preserve every raw repetition and the paired differences.

Three repetitions give a small validation study. Avoid a stable tail-latency or throughput claim. If an actual-compute counter or parent linkage is unavailable, report that limitation and the token/event evidence separately. Do not infer those measurements from CPU accounting.

The correctness check passes when the stock mismatch reproduces, each supported fixed warmup matches the real continuation prefix, unsupported cases skip, and the patch leaves the ordinary request tokens unchanged. Speed and memory are measured outcomes; a null latency result does not erase a verified rendering correction.

The shared-server contention check stays queued for a separate proposal. This rental reserves its remaining time for the two real harness sessions and teardown.

## Parity Run 1

Run one Claude Code session through `/v1/messages` and one Codex session through `/v1/responses`. Both use the same public ReedCode commit and three frozen user messages: a coding change, a test follow-up and a behavior refinement. Each has its own cold cache start. Use normal client continuation between turns, with short measured delays. Full details and exact prompts are in [Parity Run 1](parity-run-1/README.md).

Save raw harness bodies, backend input IDs, responses, final prompt/cached usage, KV events and timing. Report actual reuse, the requested input-prefix ideal and their signed difference. Show initial, within-turn and cross-turn requests separately, plus the per-session ratio of total actual to total ideal.

The protocol includes one measurement clarification: rewritten history lowers the input-prefix ideal itself. Attribute compatible cache misses and history rewrites in separate ledgers so these costs are visible without double counting. Each counted range gets one primary cause; unresolved or interacting causes stay under `other`. Include earlier computed output in a supplementary reference when raw token/event evidence supports it.

Use three percentage points as the pilot's “few points” threshold for both clients, separately within-turn and cross-turn. A complete, attributable result within that threshold ends further parity GPU work with “no significant gap observed relative to the compatible-prefix ideal.” A low ideal, missing provenance or incomplete session cannot support a broader claim. Preserve the history-rewrite audit even if cache misses are small. The hosted 85–97% range is context only.

## Approval and deliverable

The approval covers the combined run up to $5, using the local substitute above, after the pre-rental gates pass. The later retry approval permits publication of the runtime-user-only image and one replacement GPU rental. The replacement key is stored privately on the Mac. The GPU smoke checks then run inside the 35-minute readiness window. Upstream issues, PRs, code pushes and public write-ups still need review.

Return a single evidence directory with the manifest, patch hash, launch commands, raw traces, cache events, metrics, billing record, teardown confirmation and one-command report. State what the fix changed, whether the second cache branch was actually resident, and what parity Run 1 established. Bring the issue and patch back for review before filing.
