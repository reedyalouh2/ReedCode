# Questions the deployment could not answer

**Verdict: measurement gaps confirmed in the recorded deployment.** They need separate ownership checks before becoming upstream issues. The [search notes](known-issues.md) track known behavior and related work.

The [issue draft](issue-draft.md) proposes a trace join for successful warmups, with a small fix and test plan. It is ready for review as a separate issue or part of the stock-prefill report.

| Operator question | Evidence and what it blocked | Proposed next step |
| --- | --- | --- |
| Did the backend or an engine restart between counter samples? | `process_start_time_seconds` is absent from all 26 saved Prometheus snapshots. All ten replay epochs had quiet boundaries and no scrape errors, but the collector could not validate restart continuity. Server phase and counter comparisons stayed unknown. | Document the endpoint's metric families. For an owned backend, expose a process/engine generation identity or use the existing local process-identity helper. Confirm the right owner before drafting a change. |
| Which user request caused this internal warmup? | Both isolated internal requests appear in routing and backend logs. Their IDs have no entries in the saved frontend HTTP trace. The deployed source creates a new request ID with default metadata. Attribution required isolated traffic and log adjacency. | Add structured parent request ID, session ID when present, and request kind to preparation lifecycle traces. Keep IDs out of Prometheus labels. Include this in the stock-prefill fix review. |
| How much work came from speculative preparation? | Backend totals include normal and internal requests. Isolated intervals reveal the extra completed request and generated token. Concurrent totals alone cannot assign phase time to either kind. | Add bounded `request_kind` accounting and completion/cancellation outcomes, preserving an explicit unknown category. Check the current tracing path before proposing new counters. |
| Did a prepared block survive and get used later? | Runtime logs expose complete-block fingerprints; usage reports total cached tokens. Cache occupancy samples cannot identify the origin or later use of a particular block. The stock check establishes zero additional matching blocks in two examples. | Carry preparation provenance into an opt-in trace and compare hashes with the continuation. Hash compatibility, residency, and actual reuse must remain separate fields. |
| Did `session_final` release retained state? | Delivery succeeded, but the deployment did not test a configured lifecycle consumer. | Check the configured consumer and its state transitions. This record supports no cleanup failure claim. |

The restart metric is already a known integration constraint. Dynamo's metrics bridge and Prometheus multiprocess collection need to be considered together; absence alone does not establish a new serving bug. See the exact source and issue links in the search notes.

## Reproduce the record checks

```bash
uv run python experiments/dynamo-upstream/005-observability/reproduce.py
```

This uses Python's standard library. It verifies the original archive and all 102 member checksums, inspects saved metric names and epoch validation, and locates both internal requests in the logs. [results.json](results.json) records the output, script hash, deployment digest, model revision, and launch command. Raw records remain in the [September 28 archive](../../dynamo-20260928/raw-records.tar.gz). [Item 1](../001-speculative-prefill/provenance.md) gives the detailed request joins and token evidence.

## Impact and limits

The measured impact is a missing answer: ten epochs cannot support restart-validated server comparisons. There is no measured latency or throughput gain from adding these observability fields. The proposed trace joins would let an operator investigate preparation without relying on isolated log ordering; that improvement has not been implemented or tested.

These records cover one Dynamo 1.5.0 deployment with vLLM 0.28.0. They show no restart, cache-residency failure, or session-cleanup failure. Missing HTTP trace entries for internal requests are expected; the useful addition is an explicit parent/session join. Current-main observability needs a separate check. No issue has been filed.
