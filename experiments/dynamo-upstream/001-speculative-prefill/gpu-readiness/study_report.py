"""Report every replay trial and the available within-session paired differences."""

import argparse
import json
from pathlib import Path

from kv_report import report


def metrics(trial):
    followups = [r for r in trial["request_metrics"] if r["kind"] == "real"][1:]
    visible = [r["client_first_visible_text_seconds"] for r in followups]
    return {"followup_cached_tokens": sum(r["cached_tokens"] for r in followups),
            "session_scheduled_prefill_tokens": trial["scheduled_prefill_tokens"],
            "followup_completion_seconds": sum(r["completion_seconds"] for r in followups),
            "mean_followup_first_visible_text_seconds": sum(visible) / len(visible) if visible and all(v is not None for v in visible) else None,
            "terminal_warmup_only_published_entries": trial["terminal"]["counts"]["warmup_only"] if trial["attributable"] else None}


def study(directory, wire):
    trials, incomplete, grouped = [], [], {}
    paths = sorted(directory.rglob("trial.json"))
    if not paths:
        raise ValueError("No trial.json files found")
    for path in paths:
        try:
            result = report(path.parent, wire)
            result["directory"] = str(path.parent)
            key = (result["session"], result["repeat"], result["condition"])
            if key in grouped:
                raise RuntimeError("Duplicate trial cell; choose the intended study directory")
            grouped[key] = result
            trials.append(result)
        except (ValueError, KeyError, OSError) as error:
            incomplete.append({"directory": str(path.parent), "error": str(error), "error_type": type(error).__name__})
    pairs = []
    for session, repeat in sorted({(key[0], key[1]) for key in grouped}):
        for before, after in (("off", "stock"), ("stock", "fixed"), ("off", "fixed")):
            a, b = grouped.get((session, repeat, before)), grouped.get((session, repeat, after))
            if a is None or b is None:
                pairs.append({"session": session, "repeat": repeat, "comparison": after + " minus " + before,
                              "complete_pair": False})
                continue
            left, right = metrics(a), metrics(b)
            pairs.append({"session": session, "repeat": repeat, "comparison": after + " minus " + before,
                          "complete_pair": True, "before": left, "after": right,
                          "difference": {k: right[k] - left[k] if left[k] is not None and right[k] is not None else None for k in left}})
    return {"trial_directories": len(paths), "completed_reports": len(trials), "incomplete": incomplete,
            "trials": trials, "paired_differences": pairs,
            "scope": "Controlled builder replay; three repeats per session were planned. No tail-latency or throughput estimate."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--wire-decoded", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = study(args.directory, args.wire_decoded)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"completed_reports": result["completed_reports"], "incomplete": len(result["incomplete"]),
                      "paired_differences": len(result["paired_differences"])}, indent=2))
