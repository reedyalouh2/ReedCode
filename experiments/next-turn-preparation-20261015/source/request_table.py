"""Write a compact view of the foreground requests from both cohort reports."""

import argparse
import csv
import json
from pathlib import Path


def write_table(reports, destination):
    columns = [
        "cohort", "trial", "condition", "background", "task", "request_id",
        "user_turn", "input_tokens", "cached_tokens", "foreground_ceiling_tokens",
        "preparation_credit_tokens", "native_output_tokens", "native_ttft_ms",
        "client_total_ms", "finish_reason", "response_issue", "client_error",
        "turn_outcome", "session_outcome",
    ]
    with destination.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for cohort, report in reports.items():
            for trial in report["trials"]:
                sessions = {row["session_id"]: row for row in trial["sessions"]}
                for request in trial["normal_requests"]:
                    session = sessions[request["session_id"]]
                    turns = {row["turn"]: row["outcome"] for row in session.get("turns") or []}
                    ttft = request["native_first_token_latency_s"]
                    writer.writerow({
                        "cohort": cohort, "trial": trial["trial_label"],
                        "condition": trial["condition"], "background": trial["background"],
                        "task": session["task_id"], "request_id": request["request_id"],
                        "user_turn": request["user_turn"], "input_tokens": request["prompt_tokens"],
                        "cached_tokens": request["cached_tokens"],
                        "foreground_ceiling_tokens": request["foreground_history_ceiling_tokens"],
                        "preparation_credit_tokens": request["preparation_credit_lower_bound_tokens"],
                        "native_output_tokens": request["native_stats"].get("num_generation_tokens"),
                        "native_ttft_ms": ttft * 1000 if ttft is not None else None,
                        "client_total_ms": request["client_total_ms"],
                        "finish_reason": request["finish_reason"], "response_issue": request["response_issue"],
                        "client_error": request["client_error"],
                        "turn_outcome": turns.get(request["user_turn"]),
                        "session_outcome": session["outcome"],
                    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", type=Path)
    args = parser.parse_args()
    reports = {cohort: json.loads((args.reports / (cohort + ".json")).read_text())
               for cohort in ("original", "amended")}
    write_table(reports, args.reports / "requests.tsv")


if __name__ == "__main__":
    main()
