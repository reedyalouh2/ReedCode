# What priority does on vLLM

Read-only triage, September 28, 2026. Inspected Dynamo `v1.5.0` and `main` at `59ac36782c0f9662f1ab6123dce1559d1a3f377a`. No GPU test was run.

`nvext.agent_hints.priority` controls request scheduling on this path. It does not provide a supported vLLM cache-retention policy. Both versions' [backend table](https://github.com/ai-dynamo/dynamo/blob/v1.5.0/docs/fern/pages/use-cases/agents/agent-hints.md#L49-L55) mark priority-based cache eviction as **Planned** for vLLM and **Yes** for SGLang. This is documented behavior, so it does not establish a new Dynamo bug.

## Source and configuration

| Layer | Behavior | Requirement |
| --- | --- | --- |
| Dynamo API | Larger `priority` values mean higher priority. HTTP priority headers override corresponding body hints. | Keep the client value in Dynamo's convention. |
| Router | Priority can change pending-request order. `strict_priority` is a separate router-only tier. | KV routing and a `--router-queue-threshold` that actually creates a queue. |
| vLLM worker | Dynamo converts the routed value to `-int(routing.get("priority", 0))`, then passes it to generation. | Start vLLM with `--scheduling-policy priority`. |
| Cache retention | There is no supported mapping from this hint to vLLM cache-block retention priority in the documented backend table. | Do not infer retention from scheduling behavior or API acceptance. |

The v1.5 worker forwards the converted value in [token mode](https://github.com/ai-dynamo/dynamo/blob/v1.5.0/components/src/dynamo/vllm/handlers.py#L3633-L3684), [text mode](https://github.com/ai-dynamo/dynamo/blob/v1.5.0/components/src/dynamo/vllm/handlers.py#L3729-L3769), and [prefill mode](https://github.com/ai-dynamo/dynamo/blob/v1.5.0/components/src/dynamo/vllm/handlers.py#L3948-L3966). The same conversion remains in [current main](https://github.com/ai-dynamo/dynamo/blob/59ac36782c0f9662f1ab6123dce1559d1a3f377a/components/src/dynamo/vllm/handlers.py#L3794-L3846). The [priority guide](https://github.com/ai-dynamo/dynamo/blob/v1.5.0/docs/fern/pages/use-cases/agents/priority-scheduling.md) separates router, engine, and cache configuration. The [vLLM reference](https://github.com/ai-dynamo/dynamo/blob/v1.5.0/docs/fern/pages/developer-guide/knowledge-base/modular-components/backends/vllm/reference-guide.md#L50-L74) documents the engine flag and polarity conversion.

Engine scheduling can change which work runs and when resources become free. That can affect later cache residency indirectly. It does not promise that a completed high-priority request's prefix survives an eviction flood.

## Existing upstream work

- [#7492](https://github.com/ai-dynamo/dynamo/pull/7492), merged March 20, 2026, unified user-facing priority and normalized backend polarity.
- [#9821](https://github.com/ai-dynamo/dynamo/pull/9821), merged June 2, 2026, clarified layer boundaries, flags, queue requirements, and benchmark interpretation.

Three searches returned complete result sets: [`"agent_hints.priority" vllm`](https://github.com/search?type=issues&q=repo%3Aai-dynamo%2Fdynamo%20%22agent_hints.priority%22%20vllm), [`"priority" "cache eviction"`](https://github.com/search?type=issues&q=repo%3Aai-dynamo%2Fdynamo%20%22priority%22%20%22cache%20eviction%22), and [`"priority" polarity`](https://github.com/search?type=issues&q=repo%3Aai-dynamo%2Fdynamo%20%22priority%22%20polarity). They returned 1, 29, and 23 results respectively.

## Small tests worth running after review

CPU plumbing tests can use the actual preprocessor and worker with a fake downstream engine. Check that body priorities `10`, `0`, and `-10` reach `engine_client.generate` as `-10`, `0`, and `10`; verify header precedence separately. Exercise token, text, and prefill paths. Set `strict_priority` without `priority` and check that it does not become engine priority. These tests prove forwarding and conversion only. They have not been run here.

A later controlled scheduling test should hold input and output lengths constant, use equal counts of each priority, and deliberately queue requests. Test router ordering with engine scheduling held fixed, then engine scheduling with the router queue disabled. Record actual queue time and engine admissions; TTFT alone mixes both layers.

A separate retention test would warm two unique, equal-length prefixes, complete both requests, then apply the same unrelated cache-pressure traffic. Swap their priority labels and warmup order across repeats. Probe cached-token counts and block events under the same cache state. Keep probe scheduling equal and avoid interpreting probe latency as an eviction measurement. This requires an explicit GPU-run review and a pressure level that demonstrably evicts blocks.

`evidence-index.json` records hashes and search coverage. `source-evidence.tar.gz` preserves the inspected source, documentation, and report metadata.
