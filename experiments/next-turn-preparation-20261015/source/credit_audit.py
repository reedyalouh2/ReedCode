"""Recompute reported preparation credit directly from the closed engine capture."""

import argparse
import hashlib
import json
from pathlib import Path


def shared(a, b):
    return next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))


def block(count):
    return count // 16 * 16


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    entries, results, first = {}, {}, {}
    with args.engine.open() as stream:
        for line in stream:
            row = json.loads(line)
            rid = row.get("request_id")
            if row["kind"] == "engine_enter":
                entries[rid] = row
            elif row["kind"] == "engine_result":
                results[rid] = row
            elif row["kind"] == "engine_output":
                first.setdefault(rid, row)
    report = json.loads(args.report.read_text())
    assert report["worker_capture"]["valid_capture"] is True
    output = {"source_report": str(args.report), "trials": []}
    for trial in report["trials"]:
        assert trial["attribution_valid"] is True
        ordinary = [row["request_id"] for row in trial["normal_requests"]]
        preparations = [row["request_id"] for row in trial["preparation_requests"]]
        checks = []
        for row in trial["normal_requests"]:
            rid = row["request_id"]
            current = entries[rid]
            foreground = 0
            for other in ordinary:
                prior = entries[other]
                if other == rid or prior["cache_salt"] != current["cache_salt"]:
                    continue
                if prior["worker_monotonic_ns"] >= first[rid]["worker_monotonic_ns"]:
                    continue
                terminal = results[other]
                assert terminal["output_ids_status"] == "reported"
                assert not (set(terminal["outputs"]) - {"0"})
                known = prior["prompt_ids"] + terminal["outputs"].get("0", [])
                common = shared(current["prompt_ids"], known)
                # Even a naturally ended generator can be the result of an abort.
                assert not (common == len(known) < len(current["prompt_ids"])), (
                    "Unknown generated suffix could affect the ceiling", rid, other
                )
                foreground = max(foreground, block(common))
            prepared = 0
            matching_id = None
            for other in preparations:
                prior, terminal = entries[other], results[other]
                if prior["cache_salt"] != current["cache_salt"]:
                    continue
                if terminal["terminal"] != "finished" or not terminal["outputs"].get("0"):
                    continue
                if terminal["worker_monotonic_ns"] >= current["worker_monotonic_ns"]:
                    continue
                count = block(shared(current["prompt_ids"], prior["prompt_ids"]))
                if count > prepared:
                    prepared, matching_id = count, other
            cached = results[rid]["cached_tokens"]
            assert type(cached) is int and 0 <= cached <= len(current["prompt_ids"])
            credit = max(0, min(block(cached), prepared) - foreground)
            assert row["prompt_tokens"] == len(current["prompt_ids"])
            assert row["cached_tokens"] == cached
            assert row["foreground_history_ceiling_tokens"] == foreground
            assert row["matching_completed_preparation_tokens"] == prepared
            assert row["preparation_credit_lower_bound_tokens"] == credit
            checks.append({
                "request_id": rid,
                "user_turn": row["user_turn"],
                "cached_tokens": cached,
                "ordinary_history_ceiling": foreground,
                "matching_preparation_tokens": prepared,
                "matching_preparation_id": matching_id,
                "credit": credit,
                "client_error": row["client_error"],
            })
        total = sum(row["credit"] for row in checks)
        assert trial["preparation_credit_lower_bound_tokens"] == total
        output["trials"].append({
            "trial": trial["trial_label"],
            "requests_checked": len(checks),
            "partial_predecessor_sensitive_comparisons": 0,
            "credit": total,
            "credited_requests": [row for row in checks if row["credit"]],
        })
    output["requests_checked"] = sum(row["requests_checked"] for row in output["trials"])
    output["credit"] = sum(row["credit"] for row in output["trials"])
    output["source_sha256"] = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (args.engine, args.report, Path(__file__))
    }
    args.output.write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps({"requests_checked": output["requests_checked"], "credit": output["credit"]}))


if __name__ == "__main__":
    main()
