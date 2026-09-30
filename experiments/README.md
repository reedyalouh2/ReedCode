# Experiment records

| Study | Result | Evidence |
| --- | --- | --- |
| September 29: speculative prefill | 18 controlled trials. Stock added 62–85% prefill; the fix removed the separate cache branch. | [Report and reproduction](dynamo-upstream/combined-gpu-20260929/README.md) |
| September 29: harness parity | Both completed Claude Code and Codex sessions reused every compatible full prefix block. The failed Claude 32K attempt is included. | [Parity report](dynamo-upstream/combined-gpu-20260929/PARITY.md) |
| September 28: Dynamo baseline | Ten coding trials and 40 replay workflows exposed a prefix mismatch and missing restart metric. | [Baseline](dynamo-20260928/README.md) |
| September 28: prefix survey | 42 tool continuations offered a median of 80 additional preparable tokens. | [Survey](dynamo-prefix/trajectory-survey.md) |
| September 23: output retention | 15 interleaved trials comparing 20K head, 2K head and 2K head+tail. | [Results](synthetic-20260923/README.md) |
| September 21: initial pilots | Six synthetic runs and six Terminal-Bench runs, followed by a separate Codex profile. | [Earlier studies](../docs/retention-studies.md) |

The [speculative-prefill patch](dynamo-upstream/001-speculative-prefill/fix/README.md) includes CPU regressions and pinned source. Other source checks cover [rendering](dynamo-upstream/002-renderer-parity/README.md), [priority](dynamo-upstream/003-priority/README.md), [cache control](dynamo-upstream/004-cache-control/README.md), [observability](dynamo-upstream/005-observability/README.md) and [frontend registration](dynamo-upstream/006-fixed-rpc-registration/README.md).

## Recompute the retention reports

```bash
python3 summarize_ab.py
python3 summarize_real_ab.py
python3 summarize_ab.py experiments/synthetic-20260923
```

The scripts verify trace hashes and retain failed or unverified trials. [PROTOCOL.md](PROTOCOL.md) describes the retention method. GPU reproduction commands are in each study's report.

## Original pilot records

The September 21 traces predate `run_config` and `task_summary`. Agent versions come from Harbor; caps and repetitions come from filenames. The 0.1.0 source was not saved. The 0.2.0 harness is in Git history at `6e7dbc2`.

Terminal-Bench tasks were fetched at `latest`; their checksums identify what ran. Those pilots lack Git and container pins, original output sizes and truncation flags. All 20K trials preceded 2K trials. Real-suite job times total 420.006991 seconds at 20K and 346.119501 seconds at 2K.

`evals/noisy-bugfix-pilot` preserves the original fixture. The September 23 study used ReedCode 0.4.1 and the revised task, including its full pytest verifier. Its task snapshot is saved with the results. The separate Codex profile records its source-session hash and final cumulative usage; reasoning tokens are included in output tokens.
