"""Control regeneration of a self-check analysis report."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from app.controllers import distance_self_check as self_check


def _read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _apply_recovery_fields(metrics: list[dict], pass_results: list[dict]) -> None:
    observation_totals: dict[int, int] = {}
    for row in pass_results:
        pass_id = int(row["pass_id"])
        observation_totals[pass_id] = (
            observation_totals.get(pass_id, 0)
            + int(row.get("observation_count", 0))
        )
    baseline_ids = {
        int(row["pass_id"])
        for row in pass_results
        if float(row["distance_bin_start_cm"])
        == self_check.DETECTION_START_CM
        and observation_totals[int(row["pass_id"])]
        >= self_check.BASELINE_MIN_OBSERVATIONS
    }
    continuous = bool(baseline_ids)
    for metric in metrics:
        bin_start = float(metric["distance_start_cm"])
        current = {
            int(row["pass_id"]): row
            for row in pass_results
            if float(row["distance_bin_start_cm"]) == bin_start
            and observation_totals[int(row["pass_id"])]
            >= self_check.BASELINE_MIN_OBSERVATIONS
        }
        current_ids = set(current)
        missing_ids = baseline_ids - current_ids
        extra_ids = current_ids - baseline_ids
        recovered_ids = set()
        for pass_id in missing_ids:
            later = [
                row for row in pass_results
                if int(row["pass_id"]) == pass_id
                and float(row["distance_bin_start_cm"]) > bin_start
            ]
            if later and all(int(row["color_correct"]) for row in later):
                recovered_ids.add(pass_id)
        unrecovered_ids = missing_ids - recovered_ids
        present_colors_correct = all(
            int(current[pass_id]["color_correct"])
            for pass_id in baseline_ids & current_ids
        )
        qualified = (
            bool(baseline_ids)
            and not extra_ids
            and not unrecovered_ids
            and present_colors_correct
        )
        continuous = continuous and qualified
        metric["baseline_passes"] = len(baseline_ids)
        metric["missing_baseline_passes"] = len(missing_ids)
        metric["extra_pass_ids"] = len(extra_ids)
        metric["plate_id_continuity"] = int(
            bool(baseline_ids) and not missing_ids and not extra_ids
        )
        metric["color_consistent_passes"] = sum(
            int(current[pass_id]["color_correct"])
            for pass_id in baseline_ids & current_ids
        )
        metric["plate_color_consistency"] = (
            round(metric["color_consistent_passes"] / len(baseline_ids), 5)
            if baseline_ids else None
        )
        metric["range_qualified"] = int(
            metric["plate_id_continuity"] and present_colors_correct
        )
        metric["recovered_passes"] = len(recovered_ids)
        metric["recovered_pass_ids"] = " ".join(
            str(value) for value in sorted(recovered_ids)
        )
        metric["unrecovered_missing_passes"] = len(unrecovered_ids)
        metric["unrecovered_missing_pass_ids"] = " ".join(
            str(value) for value in sorted(unrecovered_ids)
        )
        metric["recoverable_range_qualified"] = int(qualified)
        metric["continuous_range_qualified"] = int(continuous)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_directory", type=Path)
    args = parser.parse_args()
    directory = args.output_directory.resolve()

    rows = _read_csv(directory / "distance_detections.csv")
    pass_results = _read_csv(directory / "pass_distance_results.csv")
    summary = json.loads(
        (directory / "distance_summary.json").read_text(encoding="utf-8")
    )
    metrics = summary["metrics"]
    _apply_recovery_fields(metrics, pass_results)

    recorder = object.__new__(self_check.SelfCheckRecorder)
    recorder.directory = directory
    recorder.rows = rows
    recorder.pass_states = {
        int(row["pass_id"]): {}
        for row in rows
    }
    report = recorder.analysis_markdown(metrics, pass_results)
    (directory / "analysis_summary.md").write_text(report, encoding="utf-8")


if __name__ == "__main__":
    main()
