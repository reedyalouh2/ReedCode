# Measuring a vLLM server

ReedCode can call vLLM through Chat Completions and sample its Prometheus metrics during inference. The adapter and collector have tests. GPU measurements and the sampler overhead check are still pending.

## Start the server

Use a dedicated Linux machine with an NVIDIA GPU. Install `vllm==0.30.0` in a separate environment, then run:

```bash
vllm serve Qwen/Qwen3-8B \
  --host 127.0.0.1 --port 8000 \
  --enable-auto-tool-choice \
  --tool-call-parser hermes \
  --reasoning-parser qwen3
```

The model and parsers follow [Qwen's deployment instructions](https://qwen.readthedocs.io/en/latest/deployment/vllm.html). Record the resolved model revision, vLLM version, GPU model/count, driver, dtype, context limit, and prefix-cache settings. Keep them fixed across conditions. The example binds to localhost; use an SSH tunnel from another machine.

Check `/v1/models` and `/metrics` first. The API model name must match the metrics' `model_name` label. Keep other clients off the server so their requests don't enter the counters.

## Run the synthetic study

From the ReedCode checkout, with port 8000 connected to the server:

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8000/v1
export OPENAI_API_KEY=unused
export VLLM_METRICS_URL=http://127.0.0.1:8000/metrics

uv run python run_experiments.py synthetic \
  --model Qwen/Qwen3-8B --model-api chat \
  --max-output-tokens 4096
```

`unused` works for a local server with authentication disabled. Otherwise, use the server's credential. The model key is never sent to the metrics endpoint. Keep both endpoints private.

This runs an oracle check followed by five repetitions of all three policies. Keep it separate from hosted-model studies. The 4,096-token budget may stop a reasoning response early; the [protocol](../experiments/PROTOCOL.md#output-token-limits) explains how those trials are scored. Check the limit-hit rate alongside passes. If the budget is too low, use a larger one in a new study.

## Read the measurements

Each inference event has a `server_metrics` object. Collection scrapes before the request, samples during it, and scrapes once more afterward. Client API latency excludes the boundary scrapes.

| Measurement | Source and meaning |
| --- | --- |
| Prefill/decode time | Differences in vLLM's request phase histogram sums and counts, in seconds |
| Queue time and TTFT | Server histogram differences; distinct from client response latency |
| Prompt/generated tokens | Server counter differences during the collection window |
| Prefix-cache hits/queries | Token counters reported by the server |
| KV cache usage | Before/after values and largest value sampled during the call, as a fraction of cache capacity |
| Running/waiting requests | Sampled gauges used to flag occupied boundaries or possible concurrent traffic |

The paired report compares summed prefill/decode seconds and the largest sampled KV fraction. Every call needs valid attribution checks; a missing or invalid measurement makes the corresponding run total unknown. KV comparisons also require an in-flight sample on every call.

The collector records missing series, counter resets, restarts, failed scrapes, and unexpected completion counts. Use per-call deltas only with a dedicated server that was idle at both boundaries. Metric definitions are in the [vLLM documentation](https://docs.vllm.ai/en/latest/usage/metrics/).

## Check sampler overhead

Warm the server, then replay fixed requests with collection alternately off and on. Keep the model, generation settings, cache policy, and workload fixed. Repeat the requests and vary the order. Compare client API latency and total elapsed time separately. Fixed requests keep agent trajectory changes out of this comparison.

## Cache policy

For a first study, keep the server running and report that its cache carries across trials. Record that policy and use it for every condition. Test restarting or clearing the cache in a separate cold-cache study.

## Limitations

These measurements cover server phase wall times and sampled cache occupancy. GPU kernel time, HBM traffic, and FLOPs are outside their scope. Prompt-token counters don't isolate newly computed KV tokens, and cache hits don't directly measure saved compute.

Sampling can miss the KV peak. With multiple engines, the collector reports the largest engine fraction; engine capacities are not used as weights. Idle boundary samples cannot prove exclusive use, and histograms published after the last scrape can prevent attribution.

The background sampler adds overhead. Complete the fixed-request check before interpreting small latency differences. Changing models, servers, or cache policies introduces differences beyond retention.
