# ReedCode

A small coding-agent harness with tool execution, per-call tracing, and Harbor integration.

I built ReedCode to study how decisions in the harness affect the workload sent to the model. The first experiment compares two limits on tool output: 20,000 and 2,000 characters. Shorter observations mean less text in subsequent requests, but they can also hide useful information and change what the agent does next.

Both settings passed the three Terminal-Bench tasks tested here. The 2K runs used 61.3% fewer cumulative input tokens and had 19.8% lower average model latency. There was only one run per task at each setting, and one task got slower. The results are small-sample observations, not a reliable speedup estimate.

## How it works

[`reedcode_harbor_agent.py`](reedcode_harbor_agent.py) calls the Responses API with a task, conversation history, and three tools: `read_file`, `write_file`, and `bash`. After each response, it executes any tool calls and appends their results to the history. It stops when the model returns without a tool call or reaches the turn limit.

The harness runs on the host. Commands run in a Docker task environment through Harbor's [`BaseEnvironment`](https://docs.harborframework.com/core-concepts/agents/custom-agents). Harbor runs the verifier after the agent finishes; the model saying it is done does not determine the reward.

Instructions and tool schemas stay fixed across calls. History includes the model's full output, including reasoning items and tool-call IDs. Tools run sequentially. There is no context compaction or parallel-agent scheduler.

Malformed arguments, invalid file paths, command failures, and recognized timeouts are returned to the model as tool results. API errors and other infrastructure failures propagate to Harbor. Cancellation also propagates so Harbor can enforce the task deadline.

The file tools check paths lexically, including absolute paths inside the workspace. This does not prevent symlink escape, and `bash` can access the rest of the container. Run the agent in disposable containers without sensitive mounts. [`agent.py`](agent.py) is the earlier local prototype; it runs commands directly on the host.

## Measurements

The harness writes a JSONL trace with token usage and latency for each model call, duration and returned bytes for each tool call, and a final task summary. Harbor records the verifier reward separately.

| Metric | Definition |
| --- | --- |
| Input, cached, and output tokens | API-reported usage summed across completed calls |
| Fresh input tokens | Input tokens minus cached input tokens |
| Model latency | Time spent awaiting complete API responses, summed per task |
| Tool latency | Time spent executing tools, summed per task |
| Tool-output bytes | UTF-8 bytes returned to the model after truncation, including formatting |
| Harbor job runtime | Time from job start to finish, including setup and verification |

Model latency includes network, service, and SDK overhead. It does not separate prefill from decode. The harness does not measure GPU time, queue time, TTFT, ITL, or KV residency. Reported cached tokens are useful workload data, but they do not tell us how much GPU compute was saved.

`MAX_TOOL_OUTPUT` keeps the first N characters of command output. The exit-code prefix and truncation marker add a little beyond that limit. Output is collected before truncation, so this setting does not bound subprocess memory use. It can cut off diagnostics at the end of an output.

## Experiments

Both suites used `gpt-5.6-terra`, with the same tools and verifiers within each suite. All 20K runs happened before the 2K runs. Oracle runs passed the local tasks and the three selected Terminal-Bench tasks before the comparisons.

### Synthetic bugfix

[`evals/noisy-bugfix`](evals/noisy-bugfix) prints 150 diagnostic lines before a failing pricing test. The agent must run the command before editing and again after fixing the bug. This deliberately tests a case with noisy output.

There were three runs per cap, using harness 0.1.0. All six passed.

| Mean per run | 20K cap | 2K cap | Change |
| --- | ---: | ---: | ---: |
| Input tokens | 13,881.7 | 5,532.0 | -60.1% |
| Cached tokens | 9,464.3 | 1,948.3 | -79.4% |
| Fresh tokens | 4,417.3 | 3,583.7 | -18.9% |
| Tool-output bytes | 18,607.7 | 5,096.7 | -72.6% |
| Model latency | 13.21 s | 11.99 s | -9.3% |
| Model calls | 6.00 | 6.00 | 0.0% |
| Tool calls | 7.00 | 6.33 | -9.5% |

### Terminal-Bench

The first batch exposed problems with absolute workspace paths, command timeouts, and reporting failed trials. After fixing those, I ran each task once per cap with harness 0.2.0. All six final runs passed, with no Harbor exceptions.

| Task | Cap | Input tokens | Fresh tokens | Model latency |
| --- | ---: | ---: | ---: | ---: |
| `extract-elf` | 20K | 45,203 | 11,022 | 64.88 s |
| `extract-elf` | 2K | 33,427 | 9,262 | 93.14 s |
| `regex-log` | 20K | 67,217 | 8,304 | 112.84 s |
| `regex-log` | 2K | 5,691 | 3,212 | 38.57 s |
| `sanitize-git-repo` | 20K | 297,390 | 39,873 | 98.23 s |
| `sanitize-git-repo` | 2K | 119,649 | 15,069 | 89.62 s |

| Mean per task | 20K cap | 2K cap | Change |
| --- | ---: | ---: | ---: |
| Input tokens | 136,603.3 | 52,922.3 | -61.3% |
| Cached tokens | 116,870.3 | 43,741.3 | -62.6% |
| Fresh tokens | 19,733.0 | 9,181.0 | -53.5% |
| Output tokens | 5,513.7 | 4,813.0 | -12.7% |
| Tool-output bytes | 39,422.7 | 10,146.7 | -74.3% |
| Model latency | 91.98 s | 73.78 s | -19.8% |
| Tool latency | 16.86 s | 8.52 s | -49.4% |
| Model calls | 12.00 | 9.33 | -22.2% |
| Tool calls | 11.00 | 9.33 | -15.2% |

Total Harbor job runtime fell from 420.01 to 346.12 seconds (17.6%), including setup and verification.

There are two useful counterexamples in the per-task results. `extract-elf` used fewer input tokens at 2K but generated more output tokens and took longer. Neither `regex-log` run even hit the smaller cap: the largest returned observations were 426 and 457 bytes. Its large improvement cannot be attributed to truncation.

Three tasks and one run per setting are not enough to separate the cap's effect from stochastic trajectories and changing service conditions. More tasks and repeated, interleaved runs would be needed. These results do not establish 2K as an optimal cap or demonstrate equivalent accuracy across Terminal-Bench.

The synthetic runs used 0.1.0, whose source snapshot was not saved. The current harness is 0.2.0. Real tasks were fetched at `latest`; their checksums are recorded, but task contents and model aliases can change. See [experiment records](experiments/README.md) for the saved traces and reproduction limits.

## Codex profile

I also profiled a separate Codex run on `terminal-bench/make-mips-interpreter` with `gpt-5.6-terra`. It made progress on a MIPS interpreter but failed the verifier, with reward 0 and no Harbor exception.

| Metric | Value |
| --- | ---: |
| Commands / failed commands | 20 / 9 |
| File-change events | 10 |
| Cumulative input tokens | 1,598,392 |
| Cached input tokens | 1,528,576 (95.63%) |
| Fresh input tokens | 69,816 |
| Output tokens | 16,912 |
| Reported reasoning output tokens | 6,332 |
| Session-reported duration | 698.05 s |
| Session-reported time to first token | 6.71 s |

This run is what made repeated context and prefix reuse interesting to investigate. These are Codex's measurements, not an improvement made by ReedCode. The TTFT value is a session field, not a per-call latency distribution.

[`profile_trajectory.py`](profile_trajectory.py) reads this session format and takes the final cumulative usage record. The [saved profile](experiments/codex_profile/profile.json) includes the source-session hash. Reasoning tokens should not be added to output tokens as a separate total.

## Running it

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and Docker for evaluations. Harbor is pinned to 0.23.0 and the OpenAI SDK to 2.54.0.

```bash
uv sync --locked

# Summarize the saved runs. No API key or Docker needed.
python3 summarize_ab.py
python3 summarize_real_ab.py

# Offline tests
uv run python -m unittest discover -s tests -v

# Check Harbor and Docker with the oracle
uv run harbor run -p evals/hello -a oracle
```

For model runs, set `OPENAI_API_KEY` in your environment. The default model is `gpt-5.6-terra`; set `MODEL` to another Responses-compatible model if your account cannot access it. Record that change when comparing results.

```bash
PYTHONPATH="$PWD" uv run harbor run -p evals/hello \
  --agent reedcode_harbor_agent:ReedCodeAgent \
  --model "${MODEL:-gpt-5.6-terra}"

# Six runs per suite; these use model API credits
bash run_ab.sh
bash run_real_ab.sh

# Use the output directory printed by the runner
python3 summarize_real_ab.py runs/real-TIMESTAMP

# Profile a compatible Codex session
python3 profile_trajectory.py /path/to/rollout.jsonl
```

New runs go under `runs/`, with full Harbor output in `jobs/`. Both directories are ignored by Git. The saved experiments are left alone. Reporters check trace hashes, retain failed attempts, and leave missing telemetry as unknown.

| Setting | Default |
| --- | ---: |
| `MAX_TURNS` | 30 model calls |
| `MAX_TOOL_OUTPUT` | 20,000 characters |
| `TOOL_TIMEOUT` | 120 seconds per command |

The experiment runner fixes the turn limit and timeout for both caps. Harbor also enforces the task's overall deadline. Settings are read at import time, so different caps need separate processes.

## Files

- `reedcode_harbor_agent.py`: the Harbor agent.
- `agent.py`: the original local prototype.
- `run_experiments.py`: runs the suites and exports results.
- `reporting.py`: shared code for the two summary scripts.
- `profile_trajectory.py`: Codex session profiler.
- `evals/`: hello smoke test and noisy bugfix task.
- `experiments/`: saved metrics and verifier results.
- `tests/`: offline harness and reporting tests.
