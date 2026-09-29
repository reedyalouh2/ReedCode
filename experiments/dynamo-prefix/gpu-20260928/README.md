# Corrected-prefix GPU check

I tested the candidate prefix on an A100 80GB through Dynamo 1.5.0 and vLLM 0.28.0, using the pinned Qwen3-8B model. The next request reused every predicted complete block. The tool example gained one block; an artificial long-argument version gained 33. The text control gained none.

| Case | Follow-up cached tokens, off | Follow-up cached tokens, prepared | Extra cached tokens |
| --- | ---: | ---: | ---: |
| Recorded tool example with fresh identifier | 224 | 240 | 16 |
| Recorded text example with fresh identifier | 80 | 80 | 0 |
| Synthetic long tool arguments | 224 | 752 | 528 |

Each off condition made two requests: the initial prompt and its follow-up. Each prepared condition added one request between them. All requests had a one-token output budget. The six conditions therefore made 15 requests and generated 15 tokens. All preparation work remains in the records.

The long case adds 512 copies of `context` to the known assistant arguments. It was constructed to test whether the boundary problem grows with assistant length. The model did not generate those arguments, and no tool executed them.

The [local audit](../README.md) builds each candidate from the completed conversation without reading the future tool result. This probe submits the token IDs through `/v1/completions`. It exercises the explicit preparation adapter. Dynamo's stock speculative-prefill implementation remains unchanged.

## Validation

Each condition starts with a fresh identifier in its first cache block. Every initial request reported zero cached tokens. Submitted token counts matched server usage, and all six follow-up hit counts matched the exact prefix prediction.

The first 15-request attempt passed the cache checks, but normal collector shutdown interrupted a process-identity read. Its server metrics remain invalid in `metrics.json`. After fixing sampler shutdown, I repeated all conditions with fresh identifiers. The second attempt passed, its epoch metrics were valid, and its backend completion counter increased by exactly 15. Both attempts are saved.

The [deployment](deployment.json) records the image digest, model revision, GPU, launch command, and collection settings. Live restart checks watched the backend process tree, engine process, Linux boot identity, and metrics listener. The model snapshot directory matched the pinned revision. The pod was deleted after downloading the records.

The observed balance change was about $0.32. Runpod reported $9.1300919287 remaining and $0/hour ongoing spend after deletion; [billing.json](billing.json) records that check.

## Limits and next step

This is a cache-mechanism check with one short example, one text control, and one synthetic extension. It measures no workflow speedup or agent quality. Fresh prefixes isolate these quiet checks; a contention study still needs verified whole-cache initialization and an overhead comparison. Request timing is retained for diagnosis only.

The result gives us a specific question: how much useful context does transcript reconstruction lose in real coding traces, and can preparing that context during tool waits help once other requests compete for the same GPU? Compare against an append-stable template as well, with a separate quality check for any prompt-format change.

## Recompute

From the repository root:

```bash
python3 experiments/dynamo-prefix/summarize_gpu.py
```

The script verifies file hashes and recomputes cache expectations from the saved token IDs and server responses. [summary.json](summary.json) contains its output. `server-records.tar.gz` holds the server logs, frontend trace, launch script, and process-identity helper.
