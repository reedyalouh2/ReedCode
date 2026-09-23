# Experiment records

## Saved studies

September 23, 2026 UTC:

- [`synthetic-20260923/`](synthetic-20260923/README.md): 15 interleaved trials on the revised synthetic task with ReedCode 0.4.1. Includes traces, task snapshot, schedule, and paired report.

September 21, 2026 UTC:

- `ab/`: six synthetic runs with ReedCode 0.1.0.
- `real_ab/`: six Terminal-Bench runs with ReedCode 0.2.0.
- `codex_profile/`: a separate Codex run on `make-mips-interpreter` that failed verification without a Harbor exception.

Manifests contain Harbor rewards and exceptions, task checksums, agent/model versions, job timestamps, and SHA-256 trace hashes. The unchanged traces contain per-call metrics. Raw sessions and Harbor jobs stay out of the repository because they contain task text, tool output, and local configuration.

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
