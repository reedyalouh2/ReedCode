# Output retention study

The [hosted synthetic study](synthetic-20260923/README.md) finished on September 23, 2026 UTC with harness 0.4.1. The ten-task study and self-hosted GPU measurements are still pending.

## Question

At a 2,000-character budget, does keeping the beginning and end of tool output preserve success or reduce extra work compared with keeping just the beginning? The third condition keeps the first 20,000 characters to test a larger budget.

## Tasks and order

I kept the three pilot tasks (`regex-log`, `extract-elf`, `sanitize-git-repo`) and added `build-cython-ext`, `build-pmars`, `cancel-async-tasks`, `custom-memory-heap-crash`, `fix-git`, `log-summary-date-ranges`, and `large-scale-text-editing`. These tasks come from the [Terminal-Bench source](https://github.com/harbor-framework/terminal-bench-2) and cover builds, debugging, logs, and file edits. The seven additions still need local oracle checks.

Each task gets five repetitions of `head_20k`, `head_2k`, and `head_tail_2k`: 150 trials. A task/repetition forms a block. The runner shuffles blocks with a saved seed and runs the three conditions consecutively within each block. For each task, it draws condition orders without replacement from the six permutations, cycling after six repetitions. The seed controls the schedule only.

The runner downloads each task once, saves its content hash, and checks that hash before every trial. All oracle checks must pass before model calls start; one failed check stops the study. Use `--check-only` to download and check tasks first. Record environment failures and task-list changes before viewing model results. Keep tasks where a policy performs badly.

The manifest records source hashes, dependency versions, model alias, schedule, task checksums, and Harbor timestamps. Use a fixed model snapshot when one is available. There are no automatic retries. Failed and interrupted attempts stay in the manifest. A dry run saves the plan without downloads or API calls; each new invocation starts a new study.

## Measurements

For each tool call, record the retention policy, budget, truncation flag, original and retained character/byte counts, and returned bytes including formatting. Keep the exit code visible under every policy. Head+tail splits the character budget evenly, with an odd extra character going to the head. The exit-code prefix and truncation marker sit outside that budget. Original sizes exclude the prefix; returned bytes include it.

Also record input, cached input, output tokens, model latency, tool latency, and verifier reward. Fresh input is input minus cached input.

## Paired analysis

Pair conditions within each task/repetition. Report candidate-minus-baseline differences for reward, input tokens, fresh input, cumulative model latency, model calls, and tool calls. The primary comparison is head+tail against head at 2K. Both comparisons against 20K are secondary.

For each task, use 10,000 bootstrap resamples of paired differences and report the 2.5th and 97.5th percentiles. Require five usable pairs for an interval and mark degenerate intervals.

Report passes against all attempts, plus missing rewards, exceptions, scheduled and attempted trials, and usable and planned pairs. Keep reward-zero trials with complete telemetry. Exclude pairs with infrastructure exceptions, missing values, or mismatched task checksums; cost comparisons also require complete telemetry. Record each exclusion reason. Missing measurements stay unknown. Check output tokens and failures alongside input use, and resolve substantial missing data before drawing efficiency conclusions.

### Output-token limits

Chat `finish_reason=length` and Responses `incomplete_details.reason=max_output_tokens` end the agent normally. Keep usage and server measurements, skip tools in the cut-off response, and let the verifier grade the workspace. Keep these trials in the analysis with their actual reward, including zero. A repair finished before the limit can still pass.

Report hits, known outcomes, and unknown outcomes per condition. Divide hits by known outcomes for the rate, and compare the binary hit indicator in paired and pooled reports. Keep the generation budget fixed throughout a study. Old exceptions with no recorded finish reason stay unclassified.

## Overall estimate

Average usable paired differences within each task, then average those task means with equal weight. Resample tasks 10,000 times for a percentile 95% interval. Require five tasks for an interval; below that, report the mean and coverage. The synthetic suite counts as one task.

Include planned and observed tasks, complete tasks, usable and planned pairs, and exclusions. Label partial coverage `observed_subset`, including when every task has some pairs but repetitions are missing.

## Server measurements

Keep hosted and self-hosted studies separate. Hold the model, server version, hardware, generation budget, sampling interval, and cache policy fixed. The [vLLM guide](../docs/self-hosted.md) defines the measurements and collection checks. Paired and pooled reports compare prefill/decode totals and sampled KV maxima only when every call has valid measurements. KV comparisons also need an in-flight sample on every call.

## Synthetic check

The revised fixture keeps pytest output after the noise and adds a compact traceback with eight cases, including fractional discounts and invalid inputs. The verifier runs a pristine copy of that suite against the edited implementation, so changing the visible tests cannot bypass verification.

`tests/check_synthetic_container.py` checks failing and passing output under all three policies. At 2K, head-only hides the pytest summary and head+tail retains it. The 20K condition keeps all of it. Keep these results separate from the earlier fixture's pilot.

## Limitations

The task list is a convenience sample. The synthetic repair is easy to solve by reading the source or rerunning focused commands. A pass alone says little about which diagnostics the agent needed. Call counts measure extra work; identifying recovery calls requires the trajectory.

Pairing holds the task and time block together, but trajectories, service conditions, and cache state still vary. Task hashes leave container images and remote dependencies unpinned. Fresh tokens don't measure GPU compute.

Five-pair intervals can be unstable or degenerate. They are exploratory and unadjusted for multiple comparisons. Equal observed rewards don't prove equal success rates. The task bootstrap describes this task mix and holds each observed task mean fixed; uncertainty within a task remains. Excluded pairs narrow the report to the observed subset.
