# Running the Dynamo baseline

These commands compare stock Dynamo with speculative prefill off and on. ReedCode keeps the first 20,000 characters of each tool result in both conditions.

The [September 28 GPU run](../experiments/dynamo-20260928/README.md) used Dynamo 1.5.0 and vLLM 0.28.0 on an A100 80GB. Ten coding trials passed and 40 replay workflows completed. It exposed a speculative-prefix mismatch and a missing restart metric.

The [September 29 follow-up](../experiments/dynamo-upstream/combined-gpu-20260929/README.md) tested the fix and captured real Claude Code and Codex sessions. Its report includes separate reproduction commands. This page covers the original baseline runner.

## Local checks

```bash
uv run python -m unittest discover -s tests
uv run python tests/check_dynamo_baseline.py
```

The second command checks streaming, session IDs, off/on metadata, and cleanup across four mock replay epochs. Requests, outcomes, and a report go under `runs/dynamo-local-check-*`, labeled as mock responses.

To inspect the real-run schedules without contacting a server:

```bash
uv run python run_dynamo.py --model Qwen/Qwen3-8B
uv run python dynamo_replay.py run experiments/dynamo/workload.json \
  --model Qwen/Qwen3-8B --copies 4
```

The included workload is a short transport fixture. Use captured coding sessions for performance work.

## Set up the GPU run

Use a dedicated Linux NVIDIA GPU server. Pin a Dynamo revision and a compatible backend from its deployment instructions. Record the actual launch commands and resolved model revision. The earlier direct-vLLM recipe is a separate deployment.

```bash
mkdir -p runs/dynamo-setup
cp experiments/dynamo/deployment.example.json runs/dynamo-setup/deployment.json
```

Fill every field from the running server: engine version, GPU model/count, dtype, context length, prefix-cache settings, and model/template options. Keep credentials out. ReedCode saves these values without checking them against the server.

Enable Dynamo request tracing with `DYN_REQUEST_TRACE=1` in the serving processes. Keep their logs with the study. Match the client session and completion IDs to the pinned server's trace format.

## Run coding tasks

With the frontend available through a local tunnel:

```bash
# Use the server's credential if authentication is enabled.
export OPENAI_API_KEY=unused
uv run python run_dynamo.py \
  --model Qwen/Qwen3-8B \
  --base-url http://127.0.0.1:8000/v1 \
  --deployment runs/dynamo-setup/deployment.json \
  --max-output-tokens 4096 --capture-requests --execute
```

This checks the task oracle, then runs five off/on pairs. Each trial has a fresh workspace and session ID. `--capture-requests` saves the full prompt, code, and tool output. Leave it out when those contents should stay out of the study files.

Once the synthetic task works, the same command accepts `--suite real --tasks regex-log extract-elf sanitize-git-repo`. Each selected task gets five pairs and an oracle check. Trials that hit the output budget still go to the verifier. Check that hit rate before drawing conclusions about retention or serving.

The study directory contains `manifest.json`, traces, `report.json`, and `report.md`. Every attempted run stays in the manifest. A missing trace makes the runner exit unsuccessfully. Rebuild a report with:

```bash
uv run python dynamo_report.py runs/STUDY
```

## Replay captured requests

Choose a normally completed trace recorded with request capture enabled:

```bash
uv run python dynamo_replay.py capture runs/STUDY/TRIAL.jsonl \
  --output runs/coding-workload.json

uv run python dynamo_replay.py run runs/coding-workload.json \
  --model Qwen/Qwen3-8B \
  --base-url http://127.0.0.1:8000/v1 \
  --deployment runs/dynamo-setup/deployment.json \
  --metrics-url http://127.0.0.1:8001/metrics \
  --copies 4 --repeats 5 --horizon-seconds 120 --execute
```

Point `--metrics-url` at the backend's actual vLLM metrics endpoint. Port 8001 above is an example tunnel. The collector currently understands vLLM metric names; leave it unset for another engine until its collector is implemented.

Replay keeps request contents, generation settings, and recorded tool waits fixed. A continuation waits for the preceding response, then its tool delay. `--copies 1` gives a quiet workload; use a separate study with more copies to introduce contention. Replay never executes shell commands from the captured conversation.

Generated responses can still differ. The report counts differences from the recorded assistant message, including reasoning and tool-call IDs. Those differences can change the usefulness of speculative prefill. Inspect them alongside latency. Cache reuse depends on the exact token prefix sent to the engine.

## What the report measures

| Field | Meaning |
| --- | --- |
| First output | Client time to the first nonempty text, reasoning, refusal, or tool-call delta; empty role chunks are ignored |
| Capped completion | Workflow arrival to final response; errors, cutoffs, and timeouts receive the full configured horizon |
| Client tokens | Reported usage, including session-final requests; unknown if any request lacks usage |
| Server prefill/decode | Histogram-sum changes over the entire epoch, including speculative requests and cleanup |
| Cache occupancy | Sampled largest engine fraction and its time integral; engine capacities are unknown |

Each session ends with a bounded minimal request carrying `X-Dynamo-Session-Final: true`. Its hint has speculation disabled. The report keeps its cost and outcome separate from agent requests; epoch counters include it. An HTTP success confirms delivery. Check the server records to verify cleanup.

After an epoch, the metrics collector waits for three quiet sampling intervals, with a ten-second drain deadline. Failed scrapes, busy boundaries, restarts, missing series, and counter resets remain visible. Keep other clients off the server and check for preparation queued beyond the drain window.

The server cache carries between conditions. Reports show paired differences and missing pairs, with equal task weights. Intervals stay unset because cache carryover is uncontrolled. Verify a reset/warmup procedure before using this runner for a confirmatory study. Sampling overhead can be checked by replaying the same workload with `--metrics-url` omitted, then supplied, while alternating the order.

## Checks to repeat on a new deployment

Follow one recorded tool turn through the server: identify the normal request, its speculative preparation, the prefix prepared, and the next request's actual reuse. Account for extra requests and generated tokens. Also check the case where speculation is off. Verify that the server acted on the hint.

Check metric availability before a longer study. The September 28 endpoint exposed vLLM counters and phase histograms but omitted `process_start_time_seconds`. The collector kept those epochs invalid and left their server comparisons unknown.

For a single owned Linux backend without that metric, start the process-identity helper after the model loads:

```bash
python metrics_identity.py --backend-pid BACKEND_PID \
  --engine-pid ENGINE_PID --metrics-port 8081 --port 9099
```

Replace the PID placeholders with the running backend and engine PIDs. Repeat `--engine-pid` for every engine. The helper binds to loopback and watches the backend process tree and metrics listener. Tunnel port 9099 alongside the metrics port, then add `--metrics-identity-url http://127.0.0.1:9099/identity` to the replay command. Both tunnels must reach the same backend.

The collector checks identity before and after each scrape. Missing responses, process changes, stale replies, and interrupted checks invalidate the epoch. Restart the helper between epochs after any process-tree change. This adds two HTTP reads per scrape; include them in the overhead check. It does not retroactively validate the archived run.

Client timing measures response delivery; server phase totals measure wall time. GPU kernel time and FLOPs are unmeasured. Whole-epoch counters include all endpoint traffic. Use real sessions to measure performance.

Protocol references: [Dynamo hints](https://docs.dynamo.nvidia.com/dynamo/agents/agent-hints), [session lifecycle](https://docs.dynamo.nvidia.com/dynamo/agents/session-i-ds), and [streamed tool calls](https://developers.openai.com/api/docs/guides/function-calling).
