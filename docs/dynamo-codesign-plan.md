# ReedCode and Dynamo co-design plan

September 27, 2026. Research proposal; no Dynamo results yet.

## The idea

Teach the serving system when an agent can make its next model call, which part of its context will survive until then, and when that plan changes.

A concrete example: an agent needs a test result and a build result before continuing. The tests finish early, but the build is still running. Preparing the agent's context at the first completion could occupy GPU memory for too long. If the build fails and the harness changes the next prompt, some preparation could also be wasted. The harness knows the dependencies and the prompt changes. The serving system knows memory pressure and the cost of preparing that context.

The thesis is that combining those two views can reduce whole-workflow latency under contention. The candidate contribution is a small, measurable interface between them, with a policy that decides when preparation is worth its cost.

This work can demonstrate strong use of Dynamo even if the first result is an upstream fix or a useful negative result. A research novelty claim needs a comparison against the work below.

## What is already covered

| Existing work | What we need to account for |
| --- | --- |
| [Dynamo agent hints](https://docs.dynamo.nvidia.com/dynamo/agents/agent-hints) | Request priority, expected output length, and speculative next-turn prefill already exist. |
| [Dynamo's experimental ThunderAgent scheduler](https://docs.dynamo.nvidia.com/dynamo/agents/thunder-agent-program-scheduler) | Program-level scheduling and tool-boundary pause/resume already exist in an experimental source component. |
| [Continuum](https://arxiv.org/abs/2511.02230) | Tool-duration predictions, KV time-to-live, and program-level scheduling are established directions. |
| [Ask the Tool, Don't Guess](https://arxiv.org/abs/2609.18849) | Live tool progress already guides cache decisions. The paper reports p90 post-tool TTFT reductions of 20.7% with HBM and 20.8% with HBM plus DRAM against LRU, and includes protection against misleading reports. |
| [KVFlow](https://arxiv.org/abs/2507.07400) | Workflow graphs already guide eviction and prefetching. |
| [PBKV](https://arxiv.org/abs/2605.06472) and [CacheScout](https://arxiv.org/abs/2608.14624) | Dynamic workflow prediction and learned execution transitions already inform cache management. |
| [Leyline](https://arxiv.org/abs/2606.01065) | Serving-side directives for editing cached context are already being studied. |
| [vLLM programmable-cache RFC](https://github.com/vllm-project/vllm/issues/57103) | Explicit cache controls across request lifetimes are under discussion. An RFC is a design proposal; implementation availability needs checking. |

These results use different workloads and hardware. ReedCode has not reproduced or beaten them. Read the methods and inspect available implementations before deciding what is new.

The narrow question to test is whether **actual next-call readiness plus prompt revision information** adds value beyond tool progress or workflow prediction alone. That combination is a candidate hypothesis. This search does not establish that it is unpublished.

## Learn enough to own the experiment

| Step | What to understand | Small check |
| --- | --- | --- |
| 1 | Prefill processes input; decode produces the response. KV cache can preserve computation for an exact shared prefix. | Explain what changes between two ReedCode turns. |
| 2 | A tool pause leaves cached state competing with other work. | Draw two agents sharing one worker while one runs tests. |
| 3 | A hint has to reach a component that acts on it. | Trace one hint through Dynamo's frontend, router, and chosen backend. |
| 4 | Cache reuse depends on tokenized prompts and model state. | Compare the prepared prefix with the real next request after chat templating. |
| 5 | A local latency improvement can delay other agents. | Explain the whole-workflow metric and the background-traffic check. |

## Start with the existing interface

The documented request fields are `nvext.agent_hints.priority`, `strict_priority`, `osl`, and `speculative_prefill`. `osl` estimates generated output tokens. It is unrelated to ReedCode's character cap on tool output. Session headers identify the reasoning chain; their presence alone does not make routing sticky. [API reference](https://docs.dynamo.nvidia.com/dynamo/agents/agent-hints)

For the first comparison, toggle only `speculative_prefill`. Keep priority, output-length settings, model, prompt policy, and generation limits fixed. Confirm that the chosen path actually prepares a prefix for a tool-calling turn. Compare that prefix with the actual next turn, including reasoning replay, tool-call IDs, and chat-template tokens. Measure any extra request and generated token used to warm it.

Pin a Dynamo source revision, compatible backend image, model revision, tokenizer, chat template, and GPU configuration before collecting results. The current ReedCode standalone vLLM pin is not a compatibility guarantee for a Dynamo release. Use one Linux GPU worker initially. Study distributed placement only after the single-worker mechanism works.

Priority needs contention at the layer using it. Cache eviction behavior also depends on backend configuration. SGLang currently has documented priority-based cache eviction support; the hint reference lists that support as planned for vLLM. Choose the backend for the operation being tested. [Priority configuration](https://docs.dynamo.nvidia.com/dynamo/agents/priority-scheduling)

## Proposed extension: a revisable next-call plan

This is a local prototype contract. These fields are **not supported Dynamo request fields**. First check whether current upstream controls can express them. If they cannot, keep the prototype in an isolated adapter and a small serving-side patch.

| Information | Who knows it | Meaning |
| --- | --- | --- |
| Session, parent, turn, and plan revision | Harness | Which intended next call this update concerns |
| Outstanding dependencies and join rule | Harness | All results required, first acceptable result, or a sequential tool bundle |
| Readiness estimate and confidence | Harness plus observed progress | A remaining-time interval based only on information available now |
| Prefix reference and expected reusable extent | Harness and server together | The context the next call is expected to retain |
| Superseded, cancelled, or finished | Harness | The old plan no longer justifies preparation |
| Cache residency, preparation cost, and load | Serving system | Whether acting on the plan is worthwhile |

A single tool completion must not imply that the next model request is ready. Sequential tools still have remaining work; an all-results join waits for its last required dependency. Unknown progress stays unknown. A test count is not automatically proportional to remaining time.

The server resolves prefix references after its own tokenization and template processing, with model, adapter, and tenant identity included. A harness character hash cannot authorize KV reuse. Tool results are unavailable until they arrive, so preparation covers only a known reusable prefix. Existing exact-prefix checks remain authoritative. If a revision changes the prompt, preparation for the superseded suffix loses its claim on resources; blocks still shared by other live work remain reusable.

Updates during a tool pause need a control path that exists independently of an inference request. A per-request Boolean cannot carry later progress updates or cancellations by itself. Dynamo's public hints also do not promise per-block TTL pinning. The [architecture discussion](https://docs.nvidia.com/dynamo/dev/digest/agentic-inference) separates current controls from future retention APIs.

Late, duplicate, or reordered updates must be harmless. A revision number and bounded expiry prevent an old completion from reviving a cancelled plan. Hints may affect performance, but must never change the prompt, execute a tool, or bypass verification.

## The first policy

Start with an understandable rule:

1. Track the dependencies that gate the next call and update its estimated ready time.
2. Resolve the reusable prefix and ask the server whether it is already resident.
3. Prepare missing state when readiness is near enough to use it and capacity permits.
4. Stop queued preparation when the plan is superseded, cancelled, or finished. Count work already spent.
5. Fall back to ordinary serving when the signal is stale, unknown, or too costly to act on.

The decision should compare expected avoided resume delay with preparation cost, occupancy while waiting, and delay imposed on other requests. Begin with measured thresholds. No learned scheduler is needed for the first test.

Cap speculative work and reserved capacity per session and globally. Separate cache-policy experiments from scheduling-priority experiments. Keep native eviction and admission behavior unless a change is an explicit experimental factor. Any synthetic warmup requests count toward cost. Reusing resident blocks, loading offloaded blocks, and recomputing missing blocks are distinct operations and need separate counters.

## Scope

Use ReedCode's current sequential tool loop first. Add an isolated workload driver for dependency patterns; the existing benchmark path stays intact. A real parallel-agent integration comes only if controlled results justify it.

Hold tool-output retention fixed at `head_20k` for the initial serving study. Record actual truncation, and keep identical observations across policies. Do not combine the existing 2K retention comparison with hint changes. Record output-token limit hits and keep their verifier outcomes as the current harness does.

Leave model training, kernel work, approximate KV reuse, and a general multi-agent framework outside this first project. Context changes in replay must be prescribed and identical across policies. A later real-agent study must use the same compaction or retry behavior in every condition.

## Baselines and ablations

| Condition | Purpose |
| --- | --- |
| Stock Dynamo KV-aware serving, speculation off | Establish the ordinary serving baseline. |
| Stock speculative prefill on | Test the existing feature fairly. |
| Tuned static preparation delay or threshold | Check whether a simple timing rule explains the gain. |
| Live-progress policy without dependency/revision information | Isolate the extra value over tool progress. |
| Workflow-aware policy without live progress/revision information | Isolate the extra value over execution-graph knowledge. |
| Proposed combined policy | Test the thesis. |
| Future-aware replay oracle | Estimate headroom using true readiness and future prefix reuse; unavailable to deployable policies. |

Also compare the experimental ThunderAgent scheduler if the claim involves program admission or scheduling. Reproduce the relevant published baseline when its implementation is usable. Otherwise label a local approximation explicitly and narrow the claim. Porting a baseline must not remove the feature it was designed to provide.

Ablate readiness information, prefix revision handling, and resource limits separately. Give baselines the same tuning budget and resource limits. Choose the strongest applicable baseline on development workloads before opening the held-out results. Compare policy variants on the same backend and the same GPU/host cache capacity.

## Workloads and measurements

Use a small controlled suite covering sequential tools, an all-results join, an early-success branch with cancellation, and a prompt revision before resumption. Include a no-revision control and a quiet-server control. Vary tool delays and context sizes using measured coding traces. Synthetic stress cases should be labeled and reported separately from realistic mixes.

ReedCode's saved traces contain metrics only. They cannot reproduce exact prompts or reconstruct tool dependencies. Capture new traces with request identity, dependencies, tool timings, and the tokenized-prefix information needed for replay. Use owned fixtures for publishable examples; keep credentials out of capture.

For replay, fix the request contents, response lengths, workflow dependencies, and exogenous tool durations across policies. Each next request becomes eligible only after its dependencies and preceding inference complete. Replaying fixed timestamps regardless of those dependencies would hide the feedback from faster serving.

Use the same external workflow arrival schedule across conditions. Interleave whole experiment epochs so policies do not compete against each other in the same cache. Give each epoch the same initial cache state and warmup policy, then let the cache evolve naturally within it. Charge all warmup and speculative work to the appropriate totals.

| Metric | Definition or purpose |
| --- | --- |
| Primary: capped workflow completion time | Arrival to successful final result, including queueing, tools, and inference; failed or unfinished workflows receive a fixed evaluation horizon |
| p90 post-tool TTFT | Time from next-request submission to first model token after a tool boundary; report harness delay separately |
| Success, timeout, and error rates | Prevent a faster set of surviving runs from looking like a win |
| Completed workflows per allocated GPU-hour | Check efficiency at the same hardware budget |
| Useful prepared KV fraction | Prepared state consumed by a later real request before eviction or invalidation |
| Wasted preparation | Prepared state never used, plus redundant transfers or recomputation |
| Prefill work and transfer bytes | Explain where a gain comes from; token counts alone are insufficient |
| Cache occupancy over time | Measure the cost of retaining prepared state during waits |
| Background p90 latency | Check whether one class improves at another's expense |
| Controller overhead | CPU, event traffic, and added time on the request/tool path |

The current `server_metrics.py` collector rejects concurrent traffic for per-call attribution. This study intentionally needs concurrency. Use request-correlated server traces for per-request measures and engine counters for whole-epoch totals, with stable session/request/speculation IDs. Do not assign a shared Prometheus counter delta to one call or disable the existing validity check to make a report pass.

Start with five independent epoch pairs for diagnosis. Freeze the policy and workload mix, then use at least ten held-out epoch pairs with enough workflow completions for tail estimates. Determine the final sample budget from development variance before the held-out run. Resample independent epochs for intervals; requests sharing a cache are correlated. Report family-level results and a predeclared weighted aggregate. Fix the evaluation horizon from development data before testing. Failed or unfinished workflows contribute that full horizon to the primary metric, even if an error ended execution early. Report their counts and actual resource use separately; do not treat an early error as a fast completion.

## What would count as a strong result

These are proposed acceptance targets. No Dynamo measurements have been collected:

- At least 15% lower mean capped workflow completion time than the strongest applicable baseline on the held-out mix, with a paired 95% interval excluding zero improvement.
- No more than 5% degradation in background p90 latency or completed workflows per GPU-hour, assessed with uncertainty rather than point estimates alone.
- No increase in timeout/error rates in replay. Real-agent validation must report verifier success and a predeclared non-inferiority margin with adequate sample size; a small all-pass sample is insufficient.
- An ablation showing that dependency/revision information contributes beyond the simpler policies, with every preparation cost included.

A large post-tool TTFT gain is useful evidence, but the primary outcome remains whole-workflow time. If only a constructed cancellation storm benefits, report that workload-specific result. If native exact-prefix caching already handles revisions cheaply, remove that part of the thesis. If the oracle has little headroom, stop before building a complex controller.

## Work in small stages

| Stage | Deliverable | Exit condition |
| --- | --- | --- |
| 1. Source and novelty audit | Pinned compatibility matrix; one traced hint path; comparison with the closest papers/RFCs | Identify an actionable gap or choose a reproduction/upstream fix. |
| 2. Existing feature on real hardware | Off/on speculative-prefill traces from a few multi-turn tool sessions | Show which exact prefix was prepared and reused, and account for its cost. |
| 3. Controlled dependency replay | Small workload driver and stock/simple-policy/oracle results | Demonstrate meaningful headroom under realistic contention. |
| 4. Minimal co-design prototype | Versioned harness events and the smallest required serving-side change | Measurable effect, correct cancellation, unchanged agent-visible behavior. |
| 5. Held-out GPU study | Full baseline comparison, ablations, intervals, and failed attempts | Meet the target or publish the measured tradeoff. |
| 6. Real coding validation | Repeated tasks with verifiers and unchanged harness semantics across conditions | Confirm the mechanism survives real trajectories. |

Use [DynoSim/Mocker](https://docs.dynamo.nvidia.com/dynamo/dev/knowledge-base/concepts/simulation/overview) for integration and hypothesis screening where its model supports the mechanism. Validate cache preparation and concurrency on a GPU early. Simulation-only gains cannot satisfy the acceptance targets.

The first implementation should add optional session/hint metadata through `model_backend.py`, record tool boundaries in `reedcode_harbor_agent.py`, and add an isolated replay driver. Do not extend the old per-call metrics analysis to concurrent studies without a separate attribution design. Keep the existing retention experiment records untouched.

Before renting hardware, record the available GPU, compatible model/backend combination, hourly rate, maximum spend, and stop time. Those details are still open. The earlier hosted API key does not provide a Dynamo GPU deployment, and this plan starts no paid run.

## What to bring to the Dynamo team

A compact artifact: the pinned deployment, a runnable workload, one chart of whole-workflow latency versus load, a cost/fairness table, and a minimal patch or API proposal. Include the case where the policy loses. Explain precisely what information the harness supplied and which serving decision changed.

If the work reproduces an existing result, call it a reproduction. If it finds a missing tool-call path or a misleading metric, submit that concrete issue with evidence. A broader contribution claim comes after the baseline comparison.
