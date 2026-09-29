# Link successful speculative prefill requests to their parent request

Local draft for review. Nothing has been filed.

## Operator question

When speculative prefill runs, which user request and session caused the internal request, and did that preparation finish? On the recorded Dynamo 1.5.0 deployment, answering this required isolated traffic and adjacent log lines.

## Reproduction

Run the [two-request stock-hint probe](../001-speculative-prefill/README.md#repeat-on-a-server) with request tracing and routing debug logs enabled. Match the normal Chat request, the internal preparation, and the follow-up. The saved reproduction used one A100, the exact image digest and command in [results.json](results.json), and Qwen3-8B revision `b968826d9c46dd6066d109eabc6255188de91218`.

The tool preparation ID is `7e0604ef-aead-454d-bf62-3172907a2c1f`. It appears at frontend log lines 108 and 112 and backend log lines 1085 and 1086. Its parent Chat request is `59fd1e70-5c6a-48a3-9642-a2305cb37fee`. The isolated text control has the same correlation gap. The [CPU record checker](reproduce.py) verifies the archive hashes and locates these records without a server.

Internal requests bypass the HTTP endpoint, so an HTTP request-trace entry is not expected. The missing field is the explicit relationship between the internal request and its parent. The [matching 1.5.0 builder](https://github.com/ai-dynamo/dynamo/blob/b83b1d9304ebfc624709ac46db32b1b6f1ff1615/lib/llm/src/preprocessor/speculative_prefill.rs) creates a fresh UUID with default metadata. The successful warmup log records only its token count.

## Related work

[#13438](https://github.com/ai-dynamo/dynamo/pull/13438) adds useful cancellation and lifetime controls, including parent request context on error paths. The [duplicate search](known-issues.md) checks current source and broader lifecycle instrumentation. The proposed addition is a join for successful preparation and its outcome.

## Proposed change

Carry the original request ID and optional session ID into the preparation task. Allocate the internal request ID before dispatch. Emit a structured lifecycle event with both IDs, request kind, prepared token count, and outcome. Preserve the current cancellation and admission controls. Reuse the existing tracing conventions and avoid request/session IDs as metric labels.

A fake downstream engine can verify the join on success, backend error, cancellation, and timeout, including concurrent parents with interleaved completions. A disabled hint should emit no preparation event. These tests and the implementation remain proposed.

## Impact and limits

This would allow direct attribution of optional work on a shared server. No latency or throughput gain is claimed. The two saved preparations demonstrate the missing join in one deployment; current-main runtime behavior has not been measured. A trace link alone does not prove that prepared blocks remained resident or were reused.
