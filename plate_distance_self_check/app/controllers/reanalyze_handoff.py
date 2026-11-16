"""Control rebuilding a handoff report from saved result rows."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

from app.views.reporting import render_handoff_report


FIELDS = [
    "upstream_session_id", "upstream_pass_id", "downstream_pass_id",
    "lap_id", "sequence", "upstream_color", "downstream_color",
    "color_correct", "handoff_status", "travel_time_s",
    "upstream_confidence", "downstream_confidence", "downstream_tracker_ids",
    "upstream_published_at", "downstream_mqtt_received_at", "mqtt_delay_ms",
    "downstream_first_seen_at", "downstream_locked_at",
]


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_directory", type=Path)
    args = parser.parse_args()
    directory = args.output_directory.resolve()
    rows = read_csv(directory / "handoff_results.csv")
    events = [
        json.loads(line)
        for line in (directory / "mqtt_events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    received = [
        event for event in events
        if event.get("direction") == "received"
        and event.get("kind") in {"plate_detected", "plate_locked"}
    ]
    represented = {
        (str(row.get("upstream_session_id", "")), str(row.get("upstream_pass_id", "")))
        for row in rows if str(row.get("upstream_pass_id", "")).strip()
    }
    for event in received:
        key = (str(event.get("session_id", "")), str(event.get("upstream_pass_id", "")))
        if key in represented:
            continue
        rows.append({
            "upstream_session_id": event.get("session_id", ""),
            "upstream_pass_id": event.get("upstream_pass_id", ""),
            "downstream_pass_id": "",
            "lap_id": event.get("lap_id", ""),
            "sequence": event.get("sequence", ""),
            "upstream_color": event.get("color", ""),
            "downstream_color": "",
            "color_correct": "",
            "handoff_status": "downstream_missing",
            "travel_time_s": "",
            "upstream_confidence": event.get("confidence", ""),
            "downstream_confidence": "",
            "downstream_tracker_ids": "",
            "upstream_published_at": event.get("timestamp", ""),
            "downstream_mqtt_received_at": event.get("received_at", ""),
            "mqtt_delay_ms": "",
            "downstream_first_seen_at": "",
            "downstream_locked_at": "",
        })
        represented.add(key)

    matched = [row for row in rows if row.get("handoff_status") == "matched"]
    correct = [row for row in matched if str(row.get("color_correct")) == "1"]
    missing = [row for row in rows if row.get("handoff_status") == "downstream_missing"]
    extra = [row for row in rows if row.get("handoff_status") == "downstream_extra"]
    travel = [float(row["travel_time_s"]) for row in matched if row.get("travel_time_s")]
    summary_path = directory / "handoff_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    sent = len(received)
    summary.update({
        "upstream_received": sent,
        "matched": len(matched),
        "downstream_missing": len(missing),
        "downstream_extra": len(extra),
        "handoff_accuracy": len(matched) / sent if sent else None,
        "color_accuracy_after_handoff": len(correct) / len(matched) if matched else None,
        "end_to_end_accuracy": len(correct) / sent if sent else None,
        "travel_time_median_s": statistics.median(travel) if travel else None,
    })
    with (directory / "handoff_results.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (directory / "handoff_analysis_summary.md").write_text(
        render_handoff_report(summary, rows), encoding="utf-8"
    )
    print(directory / "handoff_analysis_summary.md")


if __name__ == "__main__":
    main()
