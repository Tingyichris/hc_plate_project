"""CLI controller for evaluating one saved run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.models.evaluation import evaluate, parse_removal, parse_sequence
from app.views.reporting import render_evaluation_report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--sequence", required=True, help="physical O/G/Y sequence")
    parser.add_argument(
        "--removal", action="append", default=[],
        help="SEQUENCE:upstream|between|downstream[:O|G|Y]",
    )
    args = parser.parse_args()
    directory = args.output_directory.resolve()
    truth = {
        "schema_version": 1,
        "run_id": directory.name,
        "revision": 1,
        "original_sequence": parse_sequence(args.sequence),
        "removals": [parse_removal(value) for value in args.removal],
        "annotations": [],
    }
    result = evaluate(directory, truth)
    (directory / "ground_truth.json").write_text(
        json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (directory / "evaluation.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (directory / "evaluation_summary.md").write_text(
        render_evaluation_report(result), encoding="utf-8"
    )
    print(directory / "evaluation_summary.md")


if __name__ == "__main__":
    main()
