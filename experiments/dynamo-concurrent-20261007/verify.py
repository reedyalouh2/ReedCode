"""Check the published request totals and task outcomes."""

import csv
import json
from pathlib import Path


def require(condition, message):
    if not condition:
        raise SystemExit(message)


def main():
    root = Path(__file__).resolve().parent
    with (root / "requests.csv").open(newline="") as source:
        rows = list(csv.DictReader(source))
    tasks = json.loads((root / "tasks.json").read_text())
    manifest = json.loads((root / "manifest.json").read_text())
    summary = manifest["summary"]
    require(len(rows) == summary["primary_requests"] == 64, "Request count differs")
    require(len(tasks) == summary["sessions"] == 6, "Task count differs")
    require(len({(r["task_id"], r["turn_index"], r["call_index"]) for r in rows}) == 64,
            "Duplicate request row")

    for field, expected in (
        ("prompt_tokens", 447919),
        ("cached_prompt_tokens", 380512),
        ("actual_output_tokens", 90665),
    ):
        require(sum(int(r[field]) for r in rows) == expected, f"Wrong total: {field}")
    require(sum(t["tool_calls"] for t in tasks) == summary["tool_calls"] == 64,
            "Tool count differs")

    for row in rows:
        require(row["official_template_matches"] == "True", "Template mismatch")
        require(row["http_output_matches_engine"] == "True", "Output mismatch")
        gap = int(row["completed_history_prefix_tokens"]) - int(row["cached_prompt_tokens"])
        require(gap == int(row["history_minus_actual_cached_tokens"]) <= 0,
                "Cache reference differs")
    require(sum(r["output_limit_hit"] == "True" for r in rows) == 1,
            "Output-limit count differs")
    require(sorted(int(r["history_minus_actual_cached_tokens"]) for r in rows)
            == [-368, -368] + [0] * 62, "Cache differences changed")

    for phase, expected in {
        "initial": (6, 4585, 1840),
        "within_turn": (46, 377045, 343056),
        "followup": (12, 66289, 35616),
    }.items():
        group = [r for r in rows if r["phase"] == phase]
        actual = (len(group), sum(int(r["prompt_tokens"]) for r in group),
                  sum(int(r["cached_prompt_tokens"]) for r in group))
        require(actual == expected, f"Phase totals differ: {phase}")

    for task in tasks:
        group = [r for r in rows if r["task_id"] == task["task_id"]]
        require(len(group) == task["model_calls"], "Task request count differs")
        require(max(int(r["prompt_tokens"]) for r in group) == task["peak_input_tokens"],
                "Task peak input differs")
    heldout = [t["heldout_tests"] for t in tasks if t["heldout_tests"] is not None]
    require(len(heldout) == 5 and all(t["failed"] > 0 for t in heldout),
            "Held-out outcomes differ")
    require(sum(t["heldout_status"] == "heldout_patch_conflict" for t in tasks) == 1,
            "Held-out patch-conflict count differs")
    print("Verified 64 request rows, cache totals and six task outcomes.")


if __name__ == "__main__":
    main()
