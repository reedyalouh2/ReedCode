# Harness cache reuse

The [September 29 report](../combined-gpu-20260929/PARITY.md) compares actual cache hits with matching prefixes in real Claude Code and Codex sessions. Both completed sessions reused every compatible full block.

Each harness used a separate checkout of the [same frozen task](task.json), followed by [two](user-2.txt) [user follow-ups](user-3.txt). The first request used [this prompt](user-1.txt). Capture stopped at 15 model calls. Claude's initial 32K run hit its context limit; a 64K YaRN retry completed.

[clients.md](clients.md) documents the custom endpoints. [report-format.md](report-format.md) describes the capture and analysis schema. `wire-capture/decode.py` extracts token IDs from runtime traffic, `join_wire.py` joins requests to those records, and `report.py` calculates reuse. Saved raw captures and their one-command reproduction live with the report.
