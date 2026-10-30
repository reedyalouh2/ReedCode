"""Reject incomplete or failed worker captures before measuring their results."""
import argparse
import json
from pathlib import Path


def validate_capture(path, status_path=None):
    path = Path(path)
    status_path = Path(status_path) if status_path else Path(str(path) + ".close.json")
    status = json.loads(status_path.read_text())
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if status.get("valid_capture") is not True or status.get("summary_written") is not True or status.get("drain_timeout"):
        raise ValueError("Authoritative close status is invalid or incomplete")
    summaries = [row for row in rows if row.get("kind") == "writer_summary"]
    events = [row for row in rows if row.get("kind") != "writer_summary"]
    if len(summaries) != 1 or not rows or rows[-1] is not summaries[0]:
        raise ValueError("Expected exactly one terminal writer summary")
    footer = summaries[0]
    if footer.get("valid_capture") is not True:
        raise ValueError("Writer summary is invalid")
    if not events or events[0].get("kind") != "capture_start" or events[-1].get("kind") != "capture_close":
        raise ValueError("Capture start/close records are missing")
    process_id = status.get("capture_process_id")
    if not isinstance(process_id, str) or not process_id or any(row.get("capture_process_id") != process_id for row in rows):
        raise ValueError("Capture process identity differs")
    if [row.get("sequence") for row in events] != list(range(1, len(events) + 1)):
        raise ValueError("Capture event sequence is incomplete")
    for report in (status, footer):
        for name in ("attempted_records", "queued_records", "persisted_records"):
            if type(report.get(name)) is not int or report[name] != len(events):
                raise ValueError("Capture accounting differs: " + name)
        for name in ("failed_writes", "dropped_records", "pending_records"):
            if type(report.get(name)) is not int or report[name] != 0:
                raise ValueError("Capture has failures or pending records: " + name)
        if report.get("failures") != []:
            raise ValueError("Capture recorded a failure")
    return {"valid_capture": True, "capture_process_id": process_id, "event_records": len(events),
            "events_path": str(path), "close_status_path": str(status_path)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("events", type=Path)
    parser.add_argument("--status", type=Path)
    args = parser.parse_args()
    try:
        result = validate_capture(args.events, args.status)
    except (ValueError, OSError) as error:
        parser.exit(1, "Invalid worker capture: " + str(error) + "\n")
    print(json.dumps(result, sort_keys=True))
