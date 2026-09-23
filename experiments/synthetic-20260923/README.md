# Synthetic retention study, September 23, 2026

Fifteen hosted-model trials on the revised `noisy-bugfix` task: five repetitions of three retention conditions, interleaved within task/repetition blocks. All 15 scheduled trials passed the pristine eight-test verifier. No attempts were retried or omitted. There were no Harbor exceptions, missing rewards, incomplete traces, excluded pairs, or output-token limit hits; all 15 limit outcomes are known. The oracle also passed all eight tests before model calls began.

At the same 2K budget, head+tail used fewer fresh input tokens, more total input, and more tool calls than head-only. Mean API latency was lower, but its interval spans zero. This run does not establish that retaining the tail reduces work or improves accuracy.

## Configuration

| Setting | Value |
| --- | --- |
| Source commit | [`2ac14f6fadf6889ab7b28a7cacda035942c0d721`](https://github.com/reedyalouh2/ReedCode/tree/2ac14f6fadf6889ab7b28a7cacda035942c0d721) |
| Harness / Harbor / OpenAI SDK | 0.4.1 / 0.23.0 / 2.54.0 |
| Requested and returned model identifier | `gpt-5.6-terra` |
| API | Hosted OpenAI Responses |
| Conditions | `head_20k`, `head_2k`, `head_tail_2k` |
| Schedule seed | 20260922; controls ordering, not model randomness |
| Turn limit | 30 model calls |
| Command timeout | 120 seconds |
| Harbor agent deadline | 180 seconds |
| Output-token budget | Unset; provider default |
| SDK / Harbor retries | 0 / 0 |
| Server metrics | Disabled; no GPU measurements |

The runner saved the schedule before execution and checked the task snapshot before each trial. The five repetition blocks ran in order 2, 4, 3, 1, 5. The three conditions stayed adjacent within each block, with varied order. All source hashes, snapshot hashes, and trace hashes match the manifest. Every run ended with `no_tool_calls`.

The date in this directory is UTC. Harbor's original job timestamps have no timezone offset and are preserved unchanged; the host used America/Los_Angeles (September 22 locally).

## Condition means

| Mean per trial | Head 20K | Head 2K | Head+tail 2K |
| --- | ---: | ---: | ---: |
| Input tokens | 15,686.0 | 6,525.2 | 7,618.0 |
| Cached input tokens | 10,733.6 | 2,895.8 | 4,553.2 |
| Fresh input tokens | 4,952.4 | 3,629.4 | 3,064.8 |
| Output tokens | 420.4 | 389.2 | 369.4 |
| Model calls | 5.8 | 6.0 | 6.0 |
| Tool calls | 6.4 | 6.4 | 7.0 |
| Model API latency, seconds | 10.026 | 11.728 | 10.430 |
| Tool latency, seconds | 1.232 | 1.148 | 1.226 |
| Harbor job runtime, seconds | 19.127 | 20.623 | 19.454 |
| Returned tool bytes | 20,190.0 | 5,827.8 | 6,282.6 |

There were 0/32 truncated tool calls at 20K, 9/32 at head 2K, and 12/35 at head+tail 2K. Every 2K trial contained truncation. Total input fell 58.4% for head 2K and 51.4% for head+tail 2K relative to 20K. Neither 2K condition had lower mean API latency than 20K.

The study used 149,146 input tokens, including 90,913 cached tokens, and 5,895 output tokens across 89 model calls. These are API usage totals, not a billing estimate.

## Primary paired comparison

Head+tail 2K minus head 2K, with all five planned pairs included:

| Metric | Mean difference | Exploratory 95% interval |
| --- | ---: | ---: |
| Reward | 0.0 | [0.0, 0.0], degenerate |
| Input tokens | +1,092.8 | [-38.4, +2,079.2] |
| Fresh input tokens | -564.6 | [-906.0, -305.6] |
| Model API latency | -1.298 s | [-4.359, +0.813] s |
| Model calls | 0.0 | [0.0, 0.0], degenerate |
| Tool calls | +0.6 | [+0.2, +1.0] |
| Output-limit hit indicator | 0.0 | [0.0, 0.0], degenerate |

Intervals use 10,000 bootstrap resamples of the five paired differences, seed 0. The [paired report](paired_report.json) also contains both comparisons against 20K and the pooled estimates. With one task, the pooled point estimate is the same as the task estimate, and no across-task interval is reported.

Five repetitions on an easy repair cannot establish equivalent success rates or generalize to other tasks. The agent can inspect the implementation and visible tests even when pytest output is hidden. All 2K runs used six model calls; head+tail used seven tool calls in every run, while head-only used six or seven. These traces record counts and output sizes, not commands or model reasoning, so they do not label particular calls as recovery actions.

Fresh input favors head+tail here, alongside more reported cached input (4,553.2 versus 2,895.8 tokens per trial). Hosted cache state was not reset between calls. This study does not separate cache effects from trajectory differences or measure prefill, decode, or GPU time. Model identifiers are aliases, not immutable deployment versions. The saved task is fixed, but its Docker base tag remains mutable.

## Every trial

Rows follow actual execution order. Every row has reward 1, no exception, complete telemetry, and no output-limit hit.

| Block | Repeat | Condition | Input | Fresh | Output | Model calls | Tool calls | API seconds | Truncated calls |
| ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 2 | `head_2k` | 8,045 | 4,100 | 451 | 6 | 7 | 16.689 | 1 |
| 1 | 2 | `head_20k` | 15,315 | 4,723 | 405 | 6 | 7 | 10.505 | 0 |
| 1 | 2 | `head_tail_2k` | 6,960 | 2,900 | 356 | 6 | 7 | 9.644 | 2 |
| 2 | 4 | `head_tail_2k` | 7,011 | 2,915 | 358 | 6 | 7 | 10.995 | 2 |
| 2 | 4 | `head_20k` | 17,560 | 5,297 | 484 | 6 | 7 | 11.079 | 0 |
| 2 | 4 | `head_2k` | 6,237 | 3,571 | 352 | 6 | 6 | 12.236 | 2 |
| 3 | 3 | `head_20k` | 16,105 | 4,924 | 446 | 6 | 7 | 11.004 | 0 |
| 3 | 3 | `head_2k` | 5,821 | 3,311 | 421 | 6 | 7 | 10.434 | 2 |
| 3 | 3 | `head_tail_2k` | 6,975 | 2,905 | 362 | 6 | 7 | 10.020 | 2 |
| 4 | 1 | `head_tail_2k` | 8,586 | 3,306 | 393 | 6 | 7 | 11.342 | 3 |
| 4 | 1 | `head_2k` | 6,254 | 3,580 | 361 | 6 | 6 | 9.710 | 2 |
| 4 | 1 | `head_20k` | 13,486 | 4,933 | 357 | 5 | 4 | 8.058 | 0 |
| 5 | 5 | `head_2k` | 6,269 | 3,585 | 361 | 6 | 6 | 9.571 | 2 |
| 5 | 5 | `head_tail_2k` | 8,558 | 3,298 | 378 | 6 | 7 | 10.149 | 3 |
| 5 | 5 | `head_20k` | 15,964 | 4,885 | 410 | 6 | 7 | 9.483 | 0 |

## Artifacts and reproduction

- [Manifest](manifest.json): original schedule, run outcomes, oracle result, source hashes, task checksum, and trace hashes.
- Fifteen `b*.jsonl` files: unchanged per-call metric traces exported from Harbor.
- [Task snapshot](tasks/noisy-bugfix/noisy-bugfix): exact task files copied before the study. Its tree hash is in the manifest.
- [Paired report](paired_report.json): unchanged runner output, recomputed and checked against the traces.

The exported verifier rewards were checked against the original Harbor trial results, reward files, and pytest stdout; all matched. Raw Harbor jobs and local configuration remain outside the repository. No API credentials are included in these artifacts.

Recompute the report from the repository root without Docker or an API key:

```bash
python3 summarize_ab.py experiments/synthetic-20260923
```

To start a separate study using model credits, check out the source commit above, install its locked dependencies, start Docker, and provide `OPENAI_API_KEY` in the environment. Match the configuration above. This command clears endpoint and output-budget overrides and creates a new output directory:

```bash
env -u OPENAI_BASE_URL -u VLLM_METRICS_URL -u MAX_OUTPUT_TOKENS \
  uv run python run_experiments.py synthetic \
  --model gpt-5.6-terra --model-api responses --repeats 5 --seed 20260922 \
  --output runs/synthetic-new
```

New model runs will have different trajectories, cache behavior, and service latency. The ten-task study and self-hosted vLLM measurements remain unrun.
