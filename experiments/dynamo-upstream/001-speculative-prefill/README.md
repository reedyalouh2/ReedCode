# Stock speculative prefill

**Verdict: weakness confirmed on the recorded Dynamo 1.5.0 deployment.** The original isolated checks used the stock hint. The later explicit-prefix adapter was a separate experiment.

The [current-code CPU check](current-code/README.md) also reproduces both mismatches using unchanged builder fragments from the latest release and `main`, with their pinned Rust renderer and tokenizer. The [scope check](scope/README.md) confirms missing-schema failures in the tested Qwen, Nemotron and GPT-OSS tool cases.

A [local fix](fix/README.md) now supports the pinned Qwen3 tool-continuation contract. It produces exact prefixes in all 42 recorded tool continuations and 12 constructed length controls, with ordinary request tokens unchanged. The patched Dynamo crate compiles and all 19 warmup-module tests pass. The [September 29 GPU study](../combined-gpu-20260929/PREFILL.md) completed 18 controlled trials. Stock added 62.27% and 85.20% scheduled prefill work without increasing real-request cache hits. The fix removed the parallel warmup branch.

The stock tool warmup drops the tool schema and completed assistant tool call. It sends 72 tokens while the real continuation uses 244. Their reconstructed token sequences first differ at index 40; the runtime block hashes confirm divergence in that block. A text control differs at index 56 because the template treats the last assistant differently from a historical assistant.

| Case | Warm tokens | Follow-up tokens | Matching complete warm blocks | Follow-up cached tokens, off/on |
| --- | ---: | ---: | ---: | ---: |
| Tool | 72 | 244 | 2 | 192 / 192 |
| Text | 64 | 78 | 3 | 48 / 48 |

Both original stock examples receive one extra backend request for preparation without gaining another reusable full block. The later GPU run exercised both actual hint paths with generated tool calls, then compared their native-builder outputs on two saved sessions.

## Cost and scope

Across [42 recorded tool continuations](session-cost/README.md), 91,783 warmup tokens follow the first divergence from the actual next request. Prior warmups can reuse each other's incorrectly rendered history: retaining their inputs reduces modeled uncovered input to 32,733 tokens. Constructed length controls show how both quantities grow. This changes the proposed cost claim; an incompatible 50K prefix does not imply 50K fresh computation on every tool call.

The [parallel-branch calculation](kv-footprint/README.md) counts distinct full blocks retained by those inputs. Under completed-warmup and no-eviction assumptions, stock warmups add 60.7–64.7% of each session's final real-context KV size, with a 62.1% median. The five hint-off sessions use hypothetical warmups. The constructed 50K historical-tool case adds 6.8752 GiB, or 99.33% of its final real-context KV. These are CPU estimates. The later GPU event ledger measured 200 and 1,095 retained warmup-only full blocks in the short and long sessions. Their derived payload is 450 MiB and 2,463.75 MiB; exact physical allocations remain unmeasured.

The [router audit](router/README.md) finds that warmup blocks enter the normal cache-event path using their actual tokens. Matching stops at the divergent block. False cache-match credit is unconfirmed, and the inspected path rules out that specific mechanism. The GPU replay measured extra scheduled prefill. Useful-block eviction and effects on other agents remain unmeasured.

The cross-template matrix runs 16 authored cases on each revision. Restoring tools and the full assistant repairs the tested Nemotron and GPT-OSS tool prefixes. Qwen's empty-reasoning boundary still needs handling. Text boundaries vary by template; DeepSeek tool-follow-up rendering errors remain visible in the report.

## Review packet

- [Issue draft](issue-draft.md): proposed upstream report, not filed.
- [Local fix and regression evidence](fix/README.md), with the [support contract and remaining validation](proposal.md).
- [Provenance](provenance.md): exact wire bodies, source lines, internal request IDs, and limits.
- [Existing issues and PRs](known-issues.md): overlap and current-source inspection.
- [Results](results.json): machine-readable reproduction with versions and hashes.
- [Session cost](session-cost/README.md), [KV footprint](kv-footprint/README.md), [template scope](scope/README.md), and [router behavior](router/README.md): CPU evidence for impact and its limits.
- [GPU results](../combined-gpu-20260929/PREFILL.md): all 18 replay trials, per-repetition differences, branch payload and the separate failed first epoch. The [combined plan](../combined-gpu-plan.md) records the study design.
- [Raw evidence](provenance-evidence.tar.gz) and [manifest](provenance-manifest.json): byte-preserving extracts of the original records.

Related speculative-rendering problems are already discussed upstream. The draft identifies this reproduction's specific causes and links that work. It should be reviewed as supplemental evidence before deciding whether a separate issue is useful.

## Reproduce the saved finding on CPU

From the ReedCode root, using the [pinned tokenizer](../../dynamo-prefix/README.md#run-locally):

```bash
uv run --with-requirements experiments/dynamo-prefix/requirements.txt \
  python experiments/dynamo-upstream/001-speculative-prefill/reproduce.py \
  --tokenizer /tmp/reedcode-prefix-tokenizer/tokenizer.json
```

This verifies all original archive checksums, the extracted bundle against its original bytes, the received stock hints, the normal request fingerprints, and the warmed full-block hashes. It then prints the exact reconstructed differences. It makes no network requests.

The runtime did not log raw speculative token IDs. It logged lengths and hashes of complete 16-token blocks. The tool warmup's final eight tokens are outside those hashes. [results.json](results.json) distinguishes the reconstruction from observed runtime evidence.

## Repeat on a server

The saved [deployment record](../../dynamo-20260928/deployment.json) contains the original image digest, model revision, and launch command. The evidence archive also contains the debug launch script that enables routing hashes. For a fresh run, record the actual deployment and collect frontend/backend logs and request traces.

Run each condition against that isolated server:

```bash
uv run python experiments/dynamo-upstream/001-speculative-prefill/stock_probe.py \
  --base-url http://127.0.0.1:18000/v1 \
  --deployment /tmp/verified-deployment.json \
  --case tool --condition off --output /tmp/stock-tool-off

uv run python experiments/dynamo-upstream/001-speculative-prefill/stock_probe.py \
  --base-url http://127.0.0.1:18000/v1 \
  --deployment /tmp/verified-deployment.json \
  --case tool --condition on --output /tmp/stock-tool-on
```

Use `--case text` for the control. The client uses raw streamed Chat HTTP requests; it has no preparation adapter. Each invocation sends two requests and saves their complete bodies and SSE responses. A fresh identifier appears at the start of the system content, and the first response must report zero cached tokens.

The fresh probe enables speculation only on the first request, making the follow-up easier to isolate. The original archive enabled it on both. `reproduce.py` checks that original archive; it does not ingest a new probe directory. The two-second wait is only a pacing choice. Require server evidence that preparation completed before accepting a new comparison.

The September 29 run collected the [18-trial comparison](../combined-gpu-20260929/PREFILL.md). The comparison uses matched stock and fixed main frontends with the pinned vLLM backend. It measures controlled replay behavior; agent quality, shared-server latency and exact physical allocations remain unmeasured.
