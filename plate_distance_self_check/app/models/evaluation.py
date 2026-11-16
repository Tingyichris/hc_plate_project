"""Store independent ground truth and evaluate one saved handoff run."""

from __future__ import annotations

import csv
from pathlib import Path


COLOR_CODES = {
    "O": "orange",
    "G": "light_green",
    "Y": "yellow",
}
SHORT_CODES = {value: key for key, value in COLOR_CODES.items()}


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def parse_sequence(value: str) -> list[str]:
    compact = "".join(value.upper().split())
    invalid = sorted(set(compact) - set(COLOR_CODES))
    if invalid or not compact:
        raise ValueError("sequence must contain only O, G, and Y")
    return [COLOR_CODES[code] for code in compact]


def align_sequence(truth: list[str], observed: list[str]) -> list[dict]:
    """Return a deterministic minimum-edit alignment."""
    n, m = len(truth), len(observed)
    cost = [[0] * (m + 1) for _ in range(n + 1)]
    previous = [[None] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        cost[i][0] = i
        previous[i][0] = (i - 1, 0, "missed")
    for j in range(1, m + 1):
        cost[0][j] = j
        previous[0][j] = (0, j - 1, "extra")
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            same = truth[i - 1] == observed[j - 1]
            options = [
                (cost[i - 1][j - 1] + (0 if same else 1), 0,
                 i - 1, j - 1, "match" if same else "wrong_color"),
                (cost[i - 1][j] + 1, 1, i - 1, j, "missed"),
                (cost[i][j - 1] + 1, 2, i, j - 1, "extra"),
            ]
            best = min(options)
            cost[i][j] = best[0]
            previous[i][j] = (best[2], best[3], best[4])
    operations = []
    i, j = n, m
    while i or j:
        pi, pj, operation = previous[i][j]
        operations.append({
            "operation": operation,
            "truth_index": i if operation != "extra" else None,
            "observed_index": j if operation != "missed" else None,
            "truth_color": truth[i - 1] if operation != "extra" else None,
            "observed_color": observed[j - 1] if operation != "missed" else None,
        })
        i, j = pi, pj
    operations.reverse()
    return operations


def sequence_result(truth: list[str], observed: list[str]) -> dict:
    operations = align_sequence(truth, observed)
    counts = {
        name: sum(item["operation"] == name for item in operations)
        for name in ("match", "wrong_color", "missed", "extra")
    }
    return {
        "truth": [SHORT_CODES[color] for color in truth],
        "observed": [SHORT_CODES.get(color, "?") for color in observed],
        "truth_count": len(truth),
        "observed_count": len(observed),
        "count_error": len(observed) - len(truth),
        "exact_color_matches": counts["match"],
        "exact_color_rate": counts["match"] / len(truth) if truth else None,
        "wrong_color": counts["wrong_color"],
        "missed": counts["missed"],
        "extra": counts["extra"],
        "physical_identity_verified": False,
        "alignment_note": (
            "Deterministic color edit alignment only; repeated colors cannot "
            "identify which physical plate was missed without independent IDs."
        ),
        "alignment": operations,
    }


def parse_removal(value: str) -> dict:
    parts = value.split(":")
    if len(parts) not in (2, 3):
        raise ValueError("removal must be SEQUENCE:upstream|between|downstream[:O|G|Y]")
    sequence = int(parts[0])
    location = parts[1].lower()
    color = COLOR_CODES[parts[2].upper()] if len(parts) == 3 else ""
    if location == "upstream":
        result = {"location_type": "in_camera", "camera_id": "cam-3"}
    elif location == "downstream":
        result = {"location_type": "in_camera", "camera_id": "cam-2"}
    elif location == "between":
        result = {
            "location_type": "between_cameras",
            "source_camera_id": "cam-3",
            "target_camera_id": "cam-2",
        }
    else:
        raise ValueError("unknown removal location")
    return {"physical_sequence": sequence, "color": color, **result}


def removal_key(item: dict) -> tuple:
    return (
        int(item.get("physical_sequence") or 0),
        item.get("location_type", ""),
        item.get("camera_id", ""),
        item.get("source_camera_id", ""),
        item.get("target_camera_id", ""),
    )


def predicted_removals(directory: Path) -> list[dict]:
    predictions = []
    for row in read_csv(directory / "upstream_lifecycle.csv"):
        if row.get("status") == "removed":
            predictions.append({
                "physical_sequence": int(row.get("sequence") or 0),
                "color": row.get("detected_color", "unknown") or "unknown",
                "location_type": "in_camera",
                "camera_id": "cam-3",
                "source": "upstream_lifecycle.csv",
            })
    for row in read_csv(directory / "handoff_results.csv"):
        status = row.get("handoff_status")
        if status == "removed_between_cameras":
            predictions.append({
                "physical_sequence": int(row.get("sequence") or 0),
                "color": row.get("upstream_color", "unknown") or "unknown",
                "location_type": "between_cameras",
                "source_camera_id": "cam-3",
                "target_camera_id": "cam-2",
                "source": "handoff_results.csv",
            })
        elif status == "removed_at_downstream":
            predictions.append({
                "physical_sequence": int(row.get("sequence") or 0),
                "color": row.get("downstream_color", "unknown") or "unknown",
                "location_type": "in_camera",
                "camera_id": "cam-2",
                "source": "handoff_results.csv",
            })
    unique = {}
    for item in predictions:
        unique[removal_key(item)] = item
    return list(unique.values())


def safe_ratio(numerator: int, denominator: int):
    return numerator / denominator if denominator else None


def evaluate(directory: Path, truth: dict) -> dict:
    upstream_rows = read_csv(directory / "upstream_lifecycle.csv")
    upstream_rows.sort(key=lambda row: int(row.get("sequence") or 0))
    downstream_rows = read_csv(directory / "downstream_color_summary.csv")
    downstream_rows.sort(key=lambda row: int(row.get("pass_id") or 0))
    truth_sequence = list(truth["original_sequence"])
    upstream = [row.get("detected_color", "unknown") for row in upstream_rows]
    downstream = [row.get("final_color", "unknown") for row in downstream_rows]

    predictions = predicted_removals(directory)
    truth_by_key = {removal_key(item): item for item in truth.get("removals", [])}
    prediction_by_key = {removal_key(item): item for item in predictions}
    true_keys = set(truth_by_key)
    predicted_keys = set(prediction_by_key)
    tp_keys = true_keys & predicted_keys
    fp_keys = predicted_keys - true_keys
    fn_keys = true_keys - predicted_keys
    tp, fp, fn = len(tp_keys), len(fp_keys), len(fn_keys)
    precision = safe_ratio(tp, tp + fp)
    recall = safe_ratio(tp, tp + fn)
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else None
    )
    return {
        "schema_version": 1,
        "run_id": truth["run_id"],
        "ground_truth_revision": truth.get("revision", 1),
        "sequences": {
            "upstream": sequence_result(truth_sequence, upstream),
            "downstream": sequence_result(truth_sequence, downstream),
        },
        "removal_evaluation": {
            "ground_truth_count": len(true_keys),
            "predicted_count": len(predicted_keys),
            "TP": tp,
            "FP": fp,
            "FN": fn,
            "precision": precision,
            "recall": recall,
            "F1": f1,
            "count_error": len(predicted_keys) - len(true_keys),
            "false_positives": [prediction_by_key[key] for key in sorted(fp_keys)],
            "false_negatives": [truth_by_key[key] for key in sorted(fn_keys)],
        },
    }


def markdown(evaluation: dict) -> str:
    upstream = evaluation["sequences"]["upstream"]
    downstream = evaluation["sequences"]["downstream"]
    removal = evaluation["removal_evaluation"]
    ratio = lambda value: "null" if value is None else f"{value:.1%}"
    return "\n".join([
        "# Ground-truth evaluation",
        "",
        f"Run: `{evaluation['run_id']}`",
        "",
        "| Sequence | Colors | Count | Exact colors | Missed | Extra | Wrong color |",
        "|---|---|---:|---:|---:|---:|---:|",
        f"| Ground truth | {''.join(upstream['truth'])} | {upstream['truth_count']} | - | - | - | - |",
        f"| Upstream | {''.join(upstream['observed'])} | {upstream['observed_count']} | {upstream['exact_color_matches']}/{upstream['truth_count']} | {upstream['missed']} | {upstream['extra']} | {upstream['wrong_color']} |",
        f"| Downstream | {''.join(downstream['observed'])} | {downstream['observed_count']} | {downstream['exact_color_matches']}/{downstream['truth_count']} | {downstream['missed']} | {downstream['extra']} | {downstream['wrong_color']} |",
        "",
        "> Sequence alignment compares colors only. Repeated colors do not identify the exact physical plate that was missed.",
        "",
        "## Removal evaluation",
        "",
        f"- Ground truth removals: {removal['ground_truth_count']}",
        f"- Predicted removals: {removal['predicted_count']}",
        f"- TP / FP / FN: {removal['TP']} / {removal['FP']} / {removal['FN']}",
        f"- Precision / Recall / F1: {ratio(removal['precision'])} / {ratio(removal['recall'])} / {ratio(removal['F1'])}",
        "",
        "> Ground truth is stored separately from predictions. A null metric has a zero denominator.",
        "",
    ])


def main() -> None:
    """Compatibility entry point; CLI orchestration belongs to controllers."""
    from app.controllers.evaluate_run import main as controller_main

    controller_main()


if __name__ == "__main__":
    main()
