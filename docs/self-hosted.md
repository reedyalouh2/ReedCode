# Measuring a vLLM server

The harness can use vLLM's Chat Completions endpoint and sample its Prometheus metrics during each model call. This path has protocol and collector tests, but no GPU results have been collected yet.

## Start the server

Use a dedicated Linux machine with an NVIDIA GPU. Install `vllm==0.30.0` in a separate environment on that machine, then run:

```bash
vllm serve Qwen/Qwen3-8B \
  --host 127.0.0.1 --port 8000 \
  --enable-auto-tool-choice \
  --tool-call-parser hermes \
  --reasoning-parser qwen3
```

The model and parsers follow [Qwen's deployment instructions](https://qwen.readthedocs.io/en/latest/deployment/vllm.html). Record the resolved model revision, vLLM version, GPU model/count, driver, dtype, context limit, and prefix-cache settings with the run. Keep them fixed across conditions. Use an SSH tunnel if ReedCode runs on another machine; the example binds to localhost.

Check `/v1/models` and `/metrics` before running. The API model name must match the `model_name` label in the metrics. Use a server with no other clients. Server-wide counters cannot reliably separate this experiment from unrelated requests.

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

`unused` is only for a local server started without API authentication. Use the server's credential if authentication is enabled. The model API key is never forwarded to the metrics endpoint. Avoid exposing either endpoint publicly.

This runs five repetitions of all three retention policies, with an oracle check first. It is a separate study from the hosted-model pilot. Do not attribute differences between different models or servers to retention.

## Read the measurements

Each inference event contains a `server_metrics` object. The paired report compares summed prefill/decode seconds and the largest KV fraction sampled during inference; only windows with valid attribution checks contribute. A missing or invalid call makes that run's corresponding aggregate unknown. Collection uses a scrape before the API request, samples during it, and a final scrape afterward. Client API latency excludes the boundary scrapes; the background collector still adds some observer overhead. Check the overhead before using small latency differences as evidence.

| Measurement | Source and meaning |
| --- | --- |
| Prefill/decode time | Differences in vLLM's request phase histogram sums and counts, in seconds |
| Queue time and TTFT | Server histogram differences; distinct from client response latency |
| Prompt/generated tokens | Server counter differences during the collection window |
| Prefix-cache hits/queries | Token counters reported by the server |
| KV cache usage | Before/after values and largest value sampled during the call, as a fraction of cache capacity |
| Running/waiting requests | Sampled gauges used to flag occupied boundaries or possible concurrent traffic |

These are server phase wall times and cache occupancy, not GPU kernel timings, HBM traffic, or FLOPs. The sampled KV maximum can miss the actual peak. With multiple engines, the collector takes the largest reported engine fraction, not a capacity-weighted fraction for the whole server. Prompt-token totals do not isolate newly computed KV tokens. Prefix-cache hits describe reuse; they do not directly quantify saved compute. Metric definitions come from the [vLLM metrics documentation](https://docs.vllm.ai/en/latest/usage/metrics/).

Missing series, counter resets, restarts, failed scrapes, and unexpected completion counts are recorded. Missing values remain unknown. Only interpret per-call deltas when the server was dedicated and idle at the boundaries; samples alone cannot prove exclusivity. Histograms published after the final scrape may also prevent attribution.

Keep one cache policy for the entire study and record it. A persistent warm cache is a different experiment from restarting or clearing the cache between trials. Do not mix them in one comparison. For a first run, keep the server running throughout and report that the cache carries across trials. Run a separate controlled cold-cache study if needed.
