# Output retention study

Status: the [15-trial hosted synthetic study](synthetic-20260923/README.md) completed on September 23, 2026 UTC with harness 0.4.1. All scheduled trials are included. The ten-task study and self-hosted GPU measurements have not been collected.

## Question

At the same 2,000-character budget, does retaining the beginning and end of tool output preserve task success or avoid extra work compared with retaining only the beginning? A 20,000-character head-only condition measures the effect of a larger budget; it is not an unlimited-output baseline.

## Tasks and order

The proposed subset keeps the three pilot tasks (`regex-log`, `extract-elf`, `sanitize-git-repo`) and adds `build-cython-ext`, `build-pmars`, `cancel-async-tasks`, `custom-memory-heap-crash`, `fix-git`, `log-summary-date-ranges`, and `large-scale-text-editing`. These task names are listed in the [Terminal-Bench source](https://github.com/harbor-framework/terminal-bench-2). The mix includes build, debugging, log, and file work. It is a convenience sample, not a representative benchmark. The seven added tasks have not yet had local oracle checks.

The runner checks the exact task snapshots with the oracle before any model calls. A failed oracle stops the whole study; it does not silently drop that task. Use `--check-only` to download and check tasks without model calls. Record any environment failures and changes to the task list before inspecting model results. Do not drop tasks because one retention policy performs badly.

Use five repetitions per task, each with `head_20k`, `head_2k`, and `head_tail_2k`: 150 trials. The runner permutes task/repetition blocks with a saved seed. Within each block, conditions run consecutively. For each task it samples condition orders without replacement from the six possible permutations, cycling if more than six repetitions are requested. This varies order without running one condition's entire batch first. It does not eliminate temporal dependence or shared service cache effects.

Each task is downloaded once before the model runs. All conditions use that local snapshot, whose content hash is saved and checked before each trial. Source hashes, dependency versions, model alias, schedule, task checksums, and Harbor timestamps are recorded. Container images and remote dependencies can still change; task hashes alone do not pin them. Use a fixed model snapshot if the provider offers one.

There are no automatic retries. Failed and interrupted attempts remain in the manifest. A dry run saves a plan without downloads or API calls. A new invocation starts a new study; it does not resume an interrupted one.

## Measurements and analysis

Log whether retention fired on every tool call, the selected policy and budget, original and retained character/byte counts, and total bytes returned after formatting. Also record API input, cached input, output tokens, call latency, tool latency, and verifier reward. Fresh input means input minus cached input; it is not a GPU-compute estimate.

Report each task separately. Pair conditions within the same task/repetition block and compute candidate-minus-baseline differences for reward, input tokens, fresh input tokens, cumulative model latency, model calls, and tool calls. Call-count differences measure extra work; identifying a particular call as recovery requires examining the trajectory. The primary comparison is head+tail against head at 2K. The comparisons against 20K are secondary. Pairing controls task and time block; it does not give the runs identical model randomness or trajectories.

The reporter uses 10,000 bootstrap resamples of the paired differences and reports the 2.5th and 97.5th percentiles. No interval is reported below five usable pairs. Five is still a small sample: intervals can be unstable or degenerate, especially when every reward is identical. They are exploratory, unadjusted for multiple comparisons, and do not establish equivalent accuracy or generalization across tasks. Degenerate intervals are marked explicitly.

Report pass counts against all attempts, missing rewards, exceptions, scheduled versus attempted trials, and usable versus planned pairs. Reward-zero runs with complete telemetry remain in the paired analysis. Infrastructure exceptions, missing values, unfinished telemetry, and mismatched task checksums make a pair unusable; this exclusion is visible through pair coverage. Do not treat missing measurements as zero or a complete-case interval as covering failed attempts. If exclusions are material, fix the measurement problem before making an efficiency claim. Inspect output tokens and failures alongside input savings.

Chat `finish_reason=length` and Responses `incomplete_details.reason=max_output_tokens` stop the agent normally. Keep the response's usage and server measurements, skip all tool calls in that response, and run the verifier on the current workspace. These trials remain eligible for paired analysis, including when their reward is zero. A completed repair can still pass if a later response hits the limit.

Report output-limit hits by condition, the number of attempts with a known limit outcome, and the number whose outcome is unknown. The hit rate uses known outcomes as its denominator; missing outcomes must not count as non-hits. Also compare the binary limit-hit indicator in paired and pooled reports. Record the generation budget and do not change it partway through a study. Historical exceptions without a recorded finish reason cannot be reclassified reliably.

## Overall estimate

For each condition comparison and metric, first average usable paired differences within each task. Average those task means with equal weight. Resample tasks 10,000 times to obtain a percentile 95% interval; repeated trials are not counted as additional tasks. This describes the selected task mix, not all coding work. It does not remove uncertainty from having few tasks or few repeats.

Report included and planned tasks, complete tasks, usable and planned pairs, and exclusions. Partial coverage is labeled `observed_subset`, including when every task has at least one pair but some repetitions are missing. Do not present that subset as the completed ten-task study. With fewer than five tasks, show the mean and coverage without an across-task interval. In particular, the synthetic suite has one task regardless of its repetition count.

## Server measurements

With a dedicated vLLM endpoint, collect phase-time histogram differences and cache samples around each call. The paired and pooled reports can compare cumulative prefill/decode times and the maximum KV fraction sampled during inference. They exclude unattributable windows, missing measurements, counter resets, and failed scrapes. The KV comparison also requires an in-flight sample on every call. See [setup and measurement definitions](../docs/self-hosted.md). Keep model, server version, hardware, generation budget, sampling interval, and cache policy fixed. Hosted and self-hosted studies remain separate.

## Synthetic check

The earlier fixture already placed pytest output after the noise. The revision preserves that layout and uses a compact pytest traceback with eight cases, including fractional discounts and invalid inputs. The verifier runs a pristine copy of the same full suite against the edited implementation. Editing the visible tests cannot bypass it.

`tests/check_synthetic_container.py` checks actual failing and passing command output through all three retention policies. It verifies that 2K head-only hides the pytest failure/passing summary and that 2K head+tail retains it, while the 20K condition does not truncate. The harness keeps the exit code visible under every policy.

This is a diagnostic-retention check, not a difficult coding benchmark. An agent can inspect the code or rerun focused commands to recover hidden information. The completed synthetic study reports total model and tool calls; metric-only traces do not identify individual recovery actions. Do not infer information preservation from a pass alone. Keep the revised task's results separate from the saved pilot.
