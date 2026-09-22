# Evidence and provenance

These are the saved historical runs, not new measurements made during repository cleanup.

- `ab/`: six synthetic runs, ReedCode 0.1.0, September 21, 2026 UTC.
- `real_ab/`: six final real-task runs, ReedCode 0.2.0, September 21, 2026 UTC.
- `codex_profile/`: a separate Codex `make-mips-interpreter` run, reward 0, no Harbor exception.

Each A/B `manifest.json` was exported from the corresponding local Harbor job and trial `result.json`. It retains run identity, agent/model version, cap, verifier reward, exception type, task checksum, job timestamps, and a SHA-256 hash of the associated metric-only trace. Original trace bytes are unchanged. They contain per-call metrics, not model prompts, tool arguments, observations, or credentials.

The synthetic traces predate `run_config` and `task_summary`; their agent version comes from Harbor trial records. Cap and repetition come from the original runner's artifact naming. There is no preserved 0.1.0 source snapshot or original Git commit. The published harness is the existing 0.2.0 file, unchanged during publication cleanup. Running the synthetic suite now tests 0.2.0 and does not reproduce the old source revision.

The real task references were `latest`. Task checksums document the evaluated contents but are not guaranteed to be resolvable registry revisions. Exact historical reruns require matching task contents, model access, and software/runtime conditions. Container base tags and the synthetic task's pytest install were not pinned in the original task.

The Codex JSON profile records the source-session hash and selected summary fields. `profile.txt` was regenerated with `profile_trajectory.py` and checked against those fields. The raw session is omitted because it contains task and tool text. Its reported reasoning tokens should not be added to output tokens as if disjoint. The profiler supports the observed session schema; it is not a general parser for every Codex version.

## Audit without running a model

```bash
python3 summarize_ab.py
python3 summarize_real_ab.py
```

The reporters verify each trace hash, sum per-inference token usage and latency, sum per-tool post-truncation bytes and latency, and join the independent verifier reward from the manifest. Unknown fields propagate as unknown, and failed/unverified attempts are not filtered from the denominator. Relative changes compare group means, not the average of per-task percentages.

The original job directories and `.job` pointers remain outside this public copy. Full job configurations are intentionally omitted: they can carry credentials or host-specific paths. These curated exports support recomputing the claims, but are not independently signed attestations of benchmark execution.

## Historical corrections from the audit

- Exact real-suite job timestamps sum to 420.006991 and 346.119501 seconds, replacing earlier hand-transcribed estimates of 418 and 345 seconds.
- The synthetic and real suites used different harness versions and must be described separately.
- Neither `regex-log` trajectory hit the 2K cap. Its observed improvement is not evidence of a truncation effect.
