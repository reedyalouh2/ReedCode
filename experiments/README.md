# Experiment records

The active [Dynamo upstream queue](../docs/dynamo-upstream.md) packages existing evidence into reviewable findings. The speculative-prefill packet is in [`dynamo-upstream/001-speculative-prefill/`](dynamo-upstream/001-speculative-prefill/README.md). The earlier research studies are paused.

## Saved studies

September 29, 2026 UTC:

- [`dynamo-upstream/combined-gpu-20260929/`](dynamo-upstream/combined-gpu-20260929/README.md): 18 stock/fixed/off prefill trials and real Codex and Claude Code sessions. Stock warmups added prefill without additional real-request reuse. The fix removed the parallel branch. Both completed parity sessions matched the compatible-prefix reference; the incomplete Claude 32K attempt remains included.

September 28, 2026 UTC:

- [`dynamo-20260928/`](dynamo-20260928/README.md): ten coding trials and 40 replay workflows on an A100 80GB, plus isolated prefix checks. Includes server traces, metric-validation failures, and a checksummed archive.
- [`dynamo-prefix/`](dynamo-prefix/README.md): pinned-tokenizer reproduction of the mismatch, a corrected candidate, and two 15-request GPU cache probes. The repeat has valid server metrics. The long-argument case is synthetic.
- [`dynamo-prefix/trajectory-survey.md`](dynamo-prefix/trajectory-survey.md): analysis of all ten captured coding runs. Checks 52 requests against server fingerprints and measures additional preparation across 42 continuations. Includes a reproducible replay export.
- [`dynamo-prefix/boundary-detail.md`](dynamo-prefix/boundary-detail.md): exact four-token mismatch in all 42 transitions. The separate new-user probe uses constructed inputs; the [research note](../docs/dynamo-research-gap.md) distinguishes those checks from genuine session evidence.
- [`dynamo-headroom/`](dynamo-headroom/README.md): deterministic local scheduling checks with synthetic costs. These test the experiment's mechanics.

September 23, 2026 UTC:

- [`synthetic-20260923/`](synthetic-20260923/README.md): 15 interleaved trials on the revised synthetic task with ReedCode 0.4.1. Includes traces, task snapshot, schedule, and paired report.

September 21, 2026 UTC:

- `ab/`: six synthetic runs with ReedCode 0.1.0.
- `real_ab/`: six Terminal-Bench runs with ReedCode 0.2.0.
- `codex_profile/`: a separate Codex run on `make-mips-interpreter` that failed verification without a Harbor exception.

Manifests contain Harbor rewards and exceptions, task checksums, agent/model versions, job timestamps, and SHA-256 trace hashes. The unchanged traces contain per-call metrics. Harbor job directories remain local. The Dynamo archive includes captured synthetic requests and server logs.

## Recompute the reports

From the repository root:

```bash
python3 summarize_ab.py
python3 summarize_real_ab.py
python3 summarize_ab.py experiments/synthetic-20260923
```

The scripts check trace hashes, read rewards from the manifests, and keep failed or unverified attempts visible. Missing values stay unknown. Pilot percentage changes compare group means. The [protocol](PROTOCOL.md) covers analysis for the current runner.

## Pilot provenance

The old synthetic traces predate `run_config` and `task_summary`. Agent versions come from Harbor; caps and repetitions come from filenames. The 0.1.0 source was not saved. The 0.2.0 harness is in Git history at `6e7dbc2`.

The original fixture is preserved in `evals/noisy-bugfix-pilot`. The revised `evals/noisy-bugfix` adds test cases and verifies the full pytest suite. The current harness is 0.4.1. Its September 23 study uses the revised task, so the two synthetic studies stay separate.

Pilot Terminal-Bench tasks were fetched at `latest`. Their checksums identify what ran, though those versions may no longer be downloadable. The pilots lack Git commit pins, pinned container base tags, and a pinned pytest installation for the synthetic task. They also lack original output sizes and truncation flags; those fields stay unknown.

Job timestamps give real-suite totals of 420.006991 seconds at 20K and 346.119501 seconds at 2K. The README rounds these values. All 20K trials ran before 2K, leaving run order mixed into the pilot comparisons.

## Codex profile

The profile includes the source-session hash. `profile_trajectory.py` reads the final cumulative usage record; its `profile.txt` output was checked against the JSON summary. TTFT is a session field. Reasoning tokens are included in output tokens. The profiler supports the session schema used for this run.
