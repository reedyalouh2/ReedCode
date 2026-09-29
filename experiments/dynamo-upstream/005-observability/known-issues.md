# Observability duplicate check

Checked September 28, 2026. I searched public Dynamo and vLLM issues, pull requests, documentation, and source. No upstream report was submitted.

## Missing process identity

The [archived check](results.json) finds no `process_start_time_seconds` in any of the 26 saved Prometheus snapshots. All ten replay epochs therefore failed ReedCode's automatic restart check. Their idle and drain checks passed. An absent metric gives no evidence that a restart occurred.

This omission has an existing explanation. [vLLM's metrics documentation](https://github.com/vllm-project/vllm/blob/v0.28.0/docs/design/metrics.md#L77-L102) lists process metrics that are unavailable in Prometheus multiprocess mode. [Prometheus documents the underlying collector limitation](https://prometheus.github.io/client_python/multiprocess/). Its process collector also describes one process; a serving deployment can contain several independently restarting processes.

Dynamo adds another boundary: its [v1.5.0 vLLM metrics setup](https://github.com/ai-dynamo/dynamo/blob/v1.5.0/components/src/dynamo/vllm/main.py#L290-L384) forwards selected engine metric prefixes, including `vllm:` and `lmcache:`. Those exclude `process_*`. The same filtering remains in [the inspected main revision](https://github.com/ai-dynamo/dynamo/blob/59ac36782c0f9662f1ab6123dce1559d1a3f377a/components/src/dynamo/vllm/main.py#L300). Our endpoint was Dynamo's backend metrics endpoint. Describing its output as a standalone vLLM API server's complete registry would obscure this distinction.

**Owner and disposition:** ReedCode must support an explicit worker/process identity source before computing restart-sensitive deltas. A shared worker-incarnation signal would be a Dynamo observability feature to discuss with maintainers. The evidence supports a measurement-contract gap; it does not establish a new vLLM metrics regression.

Related public work:

| Source | Status checked | Relevance |
| --- | --- | --- |
| [Dynamo #3539](https://github.com/ai-dynamo/dynamo/pull/3539) | Merged October 11, 2025 | Introduces engine-metric passthrough and prefix filtering. |
| [vLLM #17546](https://github.com/vllm-project/vllm/pull/17546) | Merged May 30, 2025 | API-server scale-out; linked from the multiprocess metrics documentation. |
| [vLLM #7782](https://github.com/vllm-project/vllm/issues/7782) | Closed | Older missing-metric report about v0.5.4 engine metric families. It does not establish a process-identity regression in our deployment. |

## Joining a warmup to its originating request

The two isolated stock-hint checks recorded internal preparation IDs in frontend routing and backend completion logs. Neither ID appears in the frontend HTTP trace. The [provenance record](../001-speculative-prefill/provenance.md) joins each preparation to its preceding Chat request using the isolated log sequence. That method cannot safely attribute concurrent warmups.

An internal preparation bypasses the HTTP endpoint, so a missing HTTP payload record is expected. The useful missing field is an explicit connection between its new internal request ID, the originating request ID, and session identity.

Current source has partially improved observability. [#13438](https://github.com/ai-dynamo/dynamo/pull/13438), merged September 11, 2026, passes the originating request ID into cancellation, timeout, and error logs. In the inspected main revision, the [successful dispatch path](https://github.com/ai-dynamo/dynamo/blob/59ac36782c0f9662f1ab6123dce1559d1a3f377a/lib/llm/src/preprocessor/speculative_prefill.rs#L381-L396) still creates a new UUID with empty context metadata. Its dispatch log contains the token count. The function receives no originating request ID or session argument. This source inspection does not test current-main OTLP output.

**Owner and disposition:** Dynamo's speculative-prefill and request-tracing code owns this join. I found no exact duplicate in the bounded search below. Before filing, check the new lifecycle tracing path and ask whether it has a supported parent-request field. A focused proposal would connect both IDs and session identity, identify the request as preparation, and record completion or cancellation. It should preserve the distinction between client work and preparation work.

| Source | Status checked | Relevance |
| --- | --- | --- |
| [Dynamo #14101](https://github.com/ai-dynamo/dynamo/pull/14101) | Merged September 19, 2026 | Adds request lifecycle instrumentation with request/session identity. Its general scope should be checked before adding a separate tracing format. |
| [Dynamo #9726](https://github.com/ai-dynamo/dynamo/pull/9726) | Merged May 28, 2026 | Propagates selected HTTP metadata through the request pipeline. The speculative builder supplies an empty metadata map. |
| [Dynamo #11397](https://github.com/ai-dynamo/dynamo/issues/11397) | Closed July 14, 2026 | Lost span parentage across the Python chat-processor boundary. Our captured stock path uses the Rust preprocessor and creates a separate internal request. |
| [Dynamo #7826](https://github.com/ai-dynamo/dynamo/issues/7826) | Open | Broader request lifecycle and observability gaps after a high-concurrency failure. Relevant background; no exact warmup-parent report was identified here. |

## Search record

GitHub's issue search includes pull requests. These six queries returned complete result sets with `incomplete_results=false`. The search covered public issue/PR titles and bodies; it did not exhaust discussion comments, private tracking, or differently worded reports.

| Repository | Query terms | Returned / total |
| --- | --- | ---: |
| `ai-dynamo/dynamo` | `"process_start_time_seconds"` | 1 / 1 |
| `vllm-project/vllm` | `"process_start_time_seconds"` | 15 / 15 |
| `ai-dynamo/dynamo` | `"speculative prefill" tracing` | 14 / 14 |
| `ai-dynamo/dynamo` | `"speculative_prefill" trace` | 2 / 2 |
| `ai-dynamo/dynamo` | `"speculative prefill" observability` | 3 / 3 |
| `ai-dynamo/dynamo` | `"speculative prefill" "parent"` | 8 / 8 |

The source pin is [`59ac36782c0f9662f1ab6123dce1559d1a3f377a`](https://github.com/ai-dynamo/dynamo/commit/59ac36782c0f9662f1ab6123dce1559d1a3f377a). [search-evidence.tar.gz](search-evidence.tar.gz) preserves raw API responses, six source/document snapshots, and pull-request merge metadata. [search-evidence-index.json](search-evidence-index.json) records retrieval times, source hashes, query coverage, and the archive SHA-256. The saved deployment evidence remains in the original archive; this search ran no GPU requests.
