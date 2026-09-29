# Controlled replay handoff

Run the coordinator and KV collector on the same pod. The [launcher](../linux-readiness/README.md) supplies the pinned worker, frontend, discovery path and stable ports. Start the passive TCP capture before launching those processes. This page starts after deployment; it does not provision anything.

## Required live evidence

Before a trial, complete the generated-tool smoke on both actual hint paths. Check the worker's full-attention, BF16, TP1, 16-token block configuration and exact model revision. Save those logs. Confirm the TCP observer recovers every model request and response, and the KV collector decodes the pinned event format without gaps.

The coordinator consumes a capture-proof JSON with `token_capture_ready: true`, `kv_event_capture_ready: true`, and `worker_epoch`. That epoch must match `operator_supplied_worker_epoch` in the running KV collector's `config.json`. Keep the raw evidence behind these flags. The coordinator verifies resets independently; it cannot establish the initial deployment claims.

Use [hooks.template.json](../linux-readiness/hooks.template.json) for the reset and frontend hooks. Seed its explicit frontend PID, `/proc` start ticks and epoch as the launcher documentation requires. The fresh-frontend hook stops only that owned process, launches the selected build, clears the worker again, sends a one-token canary and requires a live successful router-clear trace with tree size zero. Each hook emits one JSON object on stdout. Hook logs and proof hashes stay with the trial.

## Actual generated-tool smoke

After your verified reset, use the corresponding stock or fixed frontend with the same backend configuration:

```bash
readiness=experiments/dynamo-upstream/001-speculative-prefill/gpu-readiness
python "$readiness/live_hint_smoke.py" --variant stock \
  --base-url http://127.0.0.1:8000/v1 \
  --output /tmp/reedcode-study/smoke-stock --execute
```

Repeat with `--variant fixed` and a fresh output directory after switching and resetting the frontend. The variant argument labels the result; deployment records establish which build ran. Without `--execute`, the driver only prints the fixture. The two variants send the same prompt and tools. Thinking stays disabled by the verified backend setting.

The model must generate exactly one `read_file` call for `project.txt`. The driver validates that call and supplies the fixed public fixture text `PROJECT_NAME=ReedCode`. It never reads a requested host path or executes a command. The exact returned assistant message enters the follow-up, which omits `nvext`. A two-second delay gives preparation time but does not prove that it ran. Each HTTP entity body, raw SSE response, observer ID, usage and timing is saved. Missing calls, cut-off responses and a non-cold initial request fail without a retry.

After decoding the passive capture with all stock frontend logs, check the actual internal warmup separately:

```bash
python "$readiness/live_hint_smoke.py" \
  --output /tmp/reedcode-study/smoke-stock \
  --check-wire /tmp/reedcode-study/wire-decoded.json
```

The offline check needs the two HTTP-to-wire links and exactly one complete one-token internal warmup admitted between them. It compares captured token arrays and saves the exact divergence index and matched full blocks. A divergent fixed warmup fails the check with its evidence preserved. The delay alone and CPU-rendered tokens cannot pass this gate.

## One trial

From the deployed repository, with the backend discovery environment exported:

```bash
readiness=experiments/dynamo-upstream/001-speculative-prefill/gpu-readiness
python "$readiness/trial.py" \
  --session long --condition stock --repeat 1 \
  --base-url http://127.0.0.1:8000/v1 \
  --metrics-url http://127.0.0.1:8081/metrics \
  --identity-url http://127.0.0.1:9099/identity \
  --hooks /tmp/reedcode-study/hooks.json \
  --capture-proof /tmp/reedcode-study/capture-proof.json \
  --kv-directory /tmp/reedcode-study/kv \
  --output /tmp/reedcode-study/trials/long-stock-r1 \
  --max-seconds 120 --execute
```

Without `--execute`, it checks the fixtures and prints its steps. Follow [schedule.json](fixtures/schedule.json) for the remaining units; failed attempts stay on disk and are not retried automatically.

Each trial first clears the worker, sends a canary and observes a new `AllBlocksCleared` event with an empty published block set. It then obtains the fresh-frontend proof and checks the second clear event. After replay, another reset and canary produce a closing clear barrier. The saved event slice includes both boundaries, so the report can verify the full sequence without guessing an event-drain delay. Canary IDs are unique to the scheduled trial.

Every model request saves its exact payload, raw SSE, usage, observer request ID and timing. Raw metrics and challenge-validated worker identities are captured before and after the request. A changed worker identity fails the trial and preserves the completed request record. These checks run while the server is dedicated to the replay.

## Decode and report

Stop the TCP observer after the final responses, saving its packet/drop counters. Decode the PCAP with the [wire decoder](../../parity-run-1/wire-capture/README.md), adding `--frontend-log PATH` for every frontend stdout JSON log. The logs join each observer header to the actual runtime request ID. Model streams must be complete. Reset-helper responses outside the selected ports remain explicitly incomplete control records.

Then build the study report in one command:

```bash
python "$readiness/study_report.py" /tmp/reedcode-study/trials \
  --wire-decoded /tmp/reedcode-study/wire-decoded.json \
  --output /tmp/reedcode-study/prefill-report.json
```

The decoder needs `msgpack==1.1.1` and `xxhash==3.5.0`; the report uses `msgpack` and the repository's existing `httpx` dependency. No renderer is substituted for captured backend token IDs.

## Accounting

For each event snapshot, the report resolves every published full block's parent chain into a token-prefix identity. It partitions entries into real-only, warmup-only, shared real/warmup, other known, and unresolved. Earlier real inputs remain in the real-history set even when later serialization changes. Missing ancestry, sequence gaps or an unexpected mid-trial clear suppress attribution or fail the report.

The warmup-branch ratio is `warmup-only published entries / full blocks in the current real input`. A second ratio uses `16 × warmup-only entries / exact current input tokens`. The current input is selected by same-pod timestamps. These are published cacheable-prefix entries; active partial allocations and physical memory residency are unmeasured.

Scheduled prefill comes from the delta of `vllm:prompt_tokens_by_source_total` with `model_name="Qwen/Qwen3-8B"`, `engine="0"`, `source="local_compute"`. The [pinned metric source](../../gpu-readiness/metric-source/manifest.json) defines this counter. The report requires matching worker identities, idle boundaries, exactly one completed request and a valid counter delta. Missing metrics produce an unknown value. Prompt tokens minus cached tokens are reported separately.

The study report retains failed/incomplete trials and pairs `stock − off`, `fixed − stock`, and `fixed − off` within each session and repetition. It compares follow-up cached tokens, scheduled prefill, completion time, first visible client text, and terminal warmup-only entries. The first-visible-text clock may omit special-token-only outputs. Three repeats cannot support a tail-latency or throughput claim. The controlled replay still omits the original assistant's decode KV, as explained in the [README](README.md).

## Switching phases on the pod

`setup_pod.py` initially launches the stock parity server with thinking enabled. After the initial live gates, `phase_control.py` handles the change to prefill and back. It only manages processes recorded in the study's ownership files. It uses Linux start ticks and the existing pidfd helper, preserves the stopped phase's logs, and never allocates or deletes a pod.

```bash
control=/tmp/reedcode-observer-venv/bin/python
"$control" "$readiness/phase_control.py" switch --phase prefill --execute
```

The switch starts fresh TCP/KV capture before the backend and frontend. It uses the existing launcher without changing model options. It records the backend process tree, requires one named `VLLM::EngineCore` descendant, validates its relationship to the metrics listener, then starts the identity service. This process layout was observed in the earlier pinned TP1 run; a different layout fails the check. Model discovery leaves the phase marked `ready_for_live_gates`, so inspect the new backend allocation and settings before continuing.

The current paths are in `/tmp/reedcode-study/phase-active.json`. Each switched phase gets its own `epochs/<id>/` directory. The bootstrap parity capture remains intact. Use the active `kv_directory`, `wire_pcap` and `processes_file` for subsequent evidence, rather than the bootstrap paths.

Run the generated stock/fixed smokes and frozen trial schedule with the recorded absolute phase deadline:

```bash
"$control" "$readiness/phase_control.py" prefill \
  --stop-at-unix "$prefill_stop_unix" --execute
```

Set `prefill_stop_unix` from the saved allocation start plus 70 minutes. The batch calls the existing reset hooks, smoke driver, wire decoder and trial coordinator. It derives capture readiness from the two actual warmup checks and a complete observed clear-to-stored KV sequence. It stops on a failed gate or trial, retains incomplete attempts, and closes request admission near the deadline. The batch always stops its owned frontend, backend tree, identity service and observers afterward. Final tcpdump counters are saved in `capture-final.json`; verify nonempty capture and zero drops before interpreting results.

Return to stock 1.5.0 and thinking enabled:

```bash
"$control" "$readiness/phase_control.py" switch --phase parity --execute
```

Recheck allocation, identity and the new phase's cache/index reset before starting either real client. The second parity session still needs its independent cold reset. The phase script does not perform those client sessions or relax their limits. `phase_control.py stop --execute` stops the currently recorded phase when needed. Omitting `--execute` from any command prints the intended action without starting processes.
