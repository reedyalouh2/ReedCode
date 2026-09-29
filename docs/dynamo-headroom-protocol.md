# Cache preparation: next experiment

This research study is paused. The short-pilot contention study is dropped. Active work follows the [upstream findings queue](dynamo-upstream.md).

The [first GPU run](../experiments/dynamo-20260928/README.md) found extra speculative requests but no extra reusable full blocks in two isolated checks. Before adding a controller, I want to establish when useful work exists to schedule.

The question is whether readiness and context changes help a serving system prepare useful cache state under pressure. There are three separate jobs: produce the right prefix, establish room for improvement, then test a policy on a GPU.

## 1. Check the prefix and the work it saves

Pin the server, model revision, tokenizer, chat template, and request settings. Capture the original input, generated assistant message, speculative input, and actual next input. Keep tool definitions, tool calls, thinking settings, and special tokens in the comparison.

Compare token IDs and complete cache blocks. The engine's block identity includes prefix lineage and any model, adapter, or isolation fields that affect reuse. A session ID or matching text fragment cannot establish reuse. Do not include unknown tool output in a prepared prefix.

Normal decoding already produces KV for the prompt and generated tokens. vLLM caches complete blocks as they fill, including during decoding. A correct speculative request can therefore repeat work already available in the cache. The final generated token and template boundaries need explicit checks. [vLLM cache design](https://docs.vllm.ai/en/latest/design/prefix_caching/)

For each continuation, record:

- Complete matching blocks in the original request plus decoding.
- Complete matching blocks in the speculative request.
- Blocks resident when preparation starts and when the real request arrives.
- Extra reusable blocks produced or restored by preparation.
- Added requests, computed tokens, cache occupancy, and latency.

**Gate:** every prepared block must be a valid prefix of the intended continuation. Then separate new computation from keeping or restoring existing KV. If a quiet worker already has the useful blocks, use that case as a negative control. Repeat under controlled eviction before assuming there is a scheduling opportunity.

## 2. Test the mechanics locally

The discrete-event sandbox uses synthetic time and capacity settings. Its outputs describe its assumptions; they are not GPU measurements. It should make bugs in the experiment visible before a rental.

Model one shared compute resource and a finite cache. Give ordinary requests and speculative work the same block service cost. Decode, demand prefill, and speculation contend for that resource. Reserve memory for active requests or explicitly limit the model to a cache-only capacity experiment; never treat active KV as free memory.

Each workflow starts at a fixed arrival time. Later tool waits start when the preceding request actually finishes. The current sandbox models sequential turns. This lets an inference improvement move the rest of the workflow forward. Fixed wall-clock timestamps for every turn would erase that effect. Dependency joins need a separate extension and tests before use.

Seed the cache with blocks produced by ordinary requests and decoding. Future tool output remains hidden from policies until its release event. An intent exposes only its currently known prefix. Revisions and cancellations become visible at their event times. The future-timing baseline may know those event times, but cannot prepare content before it is revealed.

Use three initial baselines:

| Policy | Information and action |
| --- | --- |
| Off | Ordinary caching and demand requests |
| Eager | Prepare an eligible known prefix as soon as it is available |
| Future timing | Use future readiness and validity times to choose when to prepare the same eligible prefix |

Keep cache capacity, speculative-work budget, dispatch rules, and ordinary-request priorities identical. Recheck residency and revision at dispatch. Skip already resident blocks. Charge completed speculative work even if the intent is later cancelled. Unfinished work consumes resource time until the modeled cancellation takes effect.

Future timing is a diagnostic policy. It is not an optimal bound unless the schedule is solved exhaustively. Its failure to help cannot by itself rule out every better policy.

**Gate:** pass the invariants below and retain every scenario, including losses. Only carry a mechanism to GPU testing if its benefit survives changes in capacity, wait duration, and contention. A selected synthetic example cannot support a performance claim.

## 3. Required local checks

- A fully resident quiet continuation gains no new blocks from preparation.
- Empty caches and tighter capacity affect every policy consistently.
- No cache allocation exceeds the declared capacity; active work follows the declared memory rule.
- A prepared block becomes reusable only after its computation completes.
- Two identical prefixes can share work; cancelling one owner preserves the other's claim.
- A different parent prefix prevents reuse of an otherwise identical later block.
- Unknown output cannot be prepared early, even with future timing.
- Revision and cancellation invalidate queued work; a later update cannot revive a cancelled workflow.
- Tool waits follow actual predecessor completion.
- Speculation can delay ordinary traffic and lose to the baseline.
- Charged work, occupied cache time, useful preparation, and wasted preparation reconcile with the event log.
- Repeating the same input and seed produces the same output.

Count preparation as useful only when an ordinary request consumes the prepared cache entry before it is evicted or invalidated. Count shared insertions once. Report ordinary cache hits separately. Consumption shows use; the paired baseline determines whether it reduced total work or time.

Each workflow gets the same duration limit from its own arrival. The observation window ends at the last arrival plus that limit. Cancelled and timed-out workflows receive the full capped duration. Compute charges include the full cost of work admitted before the deadline, even when it cannot finish inside the observation window. Cache occupancy includes reserved blocks and is integrated through the common observation window.

## 4. Establish GPU observability

Before timing comparisons, require all of these checks:

1. Verify the exact-prefix gate with backend token or block evidence.
2. Verify restart identity for the frontend and worker throughout an epoch. Missing restart evidence invalidates server-counter comparisons.
3. Start each independent epoch from the same declared cache state. Check the reset or restart worked, then perform identical warmup. A new session ID alone does not clear a shared prefix cache.
4. Record speculative, ordinary, and cleanup requests separately. Attribute extra work using backend evidence rather than client prompt counts.
5. Measure fixed-request replay with collection enabled and disabled. Freeze the collection settings before comparing policies.
6. Confirm the server is drained at epoch boundaries and no unrelated traffic shares the worker.

Use the same GPU, model, engine, batch settings, token budgets, request contents, and response-length policy across conditions. If fixed output lengths cannot be enforced, retain the actual lengths and report their differences. Keep engine priority constant while studying cache preparation.

## 5. Run the paired study

First measure service costs and ordinary cache behavior on the GPU. Use those measurements to choose workloads that cover both a quiet server and meaningful memory pressure. They also tell us where the local model is inaccurate.

Freeze the workload manifest, source versions, policies, limits, and analysis before the held-out run. Interleave whole epochs in a randomized order. Conditions in a pair use the same arrivals and tool durations, but their later requests remain causally tied to their own preceding completions. Reset cache state between epochs, then allow it to evolve normally within each epoch.

Use five development pairs to estimate variability and select the held-out sample size. Ten held-out pairs is a starting minimum; the required precision may need more. Bootstrap paired differences over independent epochs. Requests sharing a cache are correlated observations.

The primary outcome is mean workflow completion time, with failures and unfinished workflows assigned a fixed horizon chosen before the run. Report completion counts and failure reasons beside it. Also report post-tool TTFT, throughput, useful and wasted preparation, cache byte-seconds, and background p90 latency. Include the resource cost of control and cleanup work.

The proposed success target is a 15% reduction in mean capped completion time against the strongest applicable baseline, with a paired 95% interval excluding zero improvement. Background p90 latency and throughput should degrade by no more than 5%, with uncertainty reported. These targets are unmeasured. A live-agent follow-up needs verifier trials and a predeclared quality margin.

If future timing has useful headroom on the GPU, add fixed-delay and load-gated baselines before implementing a more complex policy. Then compare progress alone against progress plus revisions and cancellation. This identifies which signal earns its implementation cost.

## Scope of the next contribution

The current request hint does not provide a serving-side channel for updates during a tool wait. This sandbox uses an ordered event list. A real controller needs event IDs, monotonic revisions, stale and duplicate update tests, engine acknowledgements, supported-action checks, and normal-serving fallback. Simulation cannot prove an engine applied a hint.

Generic workflow-aware caching and tool-progress scheduling already have prior work. The candidate contribution is evidence that context changes and readiness together improve resource decisions in coding workloads. Compare against [KVFlow](https://arxiv.org/abs/2507.07400), [CacheScout](https://arxiv.org/abs/2608.14624), and [tool-progress scheduling](https://arxiv.org/abs/2609.18849), and fit the integration to Dynamo's [session-aware prefix indexer](https://github.com/ai-dynamo/dynamo/issues/13279).
