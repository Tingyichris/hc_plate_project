"""Structured accuracy logging for the live bbox plate detector.

The recorder deliberately keeps raw observations as CSV as well as producing a
session summary.  Ground-truth add/remove events are entered by the operator;
latency cannot be measured reliably from model output alone.
"""

from __future__ import annotations

import csv
import json
import math
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from . import constants as const


CSV_FIELDS = [
    "session_s", "frame_index", "inference_index", "inference_ms",
    "tracker_id", "plate_id", "confidence", "current_color", "stable_color",
    "x1", "y1", "x2", "y2", "center_x", "center_y", "width", "height",
    "duplicate_count", "is_reappearance", "color_switched",
    "progress", "batch_id", "sequence", "expected_color", "stage",
]

EXPECTED_SEQUENCE = ("red", "light_green", "orange")
ENTRY_LINE_RATIO = 0.85
COUNTING_LINE_RATIO = 0.55
EXIT_LINE_RATIO = 0.15


@dataclass
class PlateState:
    plate_id: int
    tracker_ids: set[int] = field(default_factory=set)
    first_seen: float = 0.0
    last_seen: float = 0.0
    last_box: tuple[float, float, float, float] | None = None
    last_color: str = "unknown"
    observations: int = 0
    disappearances: int = 0
    reappearances: int = 0
    color_switches: int = 0
    duplicate_frames: int = 0
    last_progress: float | None = None
    batch_id: int | None = None
    sequence: int | None = None
    expected_color: str = ""
    stage: str = "approaching"
    entry_time: float | None = None
    entry_color: str = ""
    count_time: float | None = None
    count_color: str = ""
    exit_time: float | None = None
    exit_color: str = ""
    exit_candidate: bool = False
    caught_time: float | None = None


class BBoxAccuracyRecorder:
    """Record detections and derive identity, timing, duplicate and heat metrics."""

    def __init__(self, root="outputs/bbox_accuracy", reconnect_seconds=2.0):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.directory = Path(root) / stamp
        self.directory.mkdir(parents=True, exist_ok=True)
        self.started = time.perf_counter()
        self.reconnect_seconds = reconnect_seconds
        self.inference_index = 0
        self.next_plate_id = 1
        self.states: dict[int, PlateState] = {}
        self.tracker_to_plate: dict[int, int] = {}
        self.visible_last: set[int] = set()
        self.rows: list[dict] = []
        self.frame_rows: list[dict] = []
        self.events: list[dict] = []
        self.crossing_events: list[dict] = []
        self.batches: dict[int, dict] = {}
        self.active_batch_id = 0
        self.manual_open: list[dict] = []
        self.frame_shape = None
        self.error_points: list[tuple[int, int, float]] = []
        self.finalized = False
        self.start_batch()

    def _now(self):
        return time.perf_counter() - self.started

    @staticmethod
    def _center(box):
        return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)

    @staticmethod
    def _match_score(first, second):
        ax, ay = BBoxAccuracyRecorder._center(first)
        bx, by = BBoxAccuracyRecorder._center(second)
        scale = max(20.0, math.hypot(second[2] - second[0], second[3] - second[1]))
        return math.hypot(ax - bx, ay - by) / scale

    def _plate_for(self, tracker_id, box, now):
        if tracker_id in self.tracker_to_plate:
            return self.tracker_to_plate[tracker_id], False
        candidates = []
        for plate_id, state in self.states.items():
            # EXIT is the terminal boundary.  A detection above it must never
            # revive a plate whose result has already been finalized.
            if state.exit_time is not None or state.caught_time is not None:
                continue
            gap = now - state.last_seen
            if state.last_box is None or gap <= 0 or gap > self.reconnect_seconds:
                continue
            score = self._match_score(box, state.last_box)
            if score <= 1.5:
                candidates.append((score, plate_id))
        if candidates:
            plate_id = min(candidates)[1]
            self.states[plate_id].reappearances += 1
            reappeared = True
        else:
            plate_id = self.next_plate_id
            self.next_plate_id += 1
            self.states[plate_id] = PlateState(plate_id=plate_id, first_seen=now)
            reappeared = False
        self.tracker_to_plate[tracker_id] = plate_id
        return plate_id, reappeared

    @staticmethod
    def _duplicate_counts(instances):
        counts = [0] * len(instances)
        for i, left in enumerate(instances):
            a = left["xyxy"]
            for j in range(i + 1, len(instances)):
                b = instances[j]["xyxy"]
                ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
                iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
                intersection = ix * iy
                smaller = min(max(1.0, (a[2]-a[0])*(a[3]-a[1])),
                              max(1.0, (b[2]-b[0])*(b[3]-b[1])))
                if intersection / smaller >= 0.45:
                    counts[i] += 1
                    counts[j] += 1
        return counts

    def start_batch(self):
        now = self._now()
        if self.active_batch_id in self.batches:
            self.batches[self.active_batch_id]["closed_s"] = round(now, 4)
        self.active_batch_id += 1
        self.batches[self.active_batch_id] = {
            "batch_id": self.active_batch_id,
            "started_s": round(now, 4),
            "closed_s": "",
            "entry_ids": [],
            "exit_ids": [],
        }
        print(
            f"Started batch {self.active_batch_id}: "
            "expected red -> light_green -> orange"
        )

    @staticmethod
    def _crossed(previous, current, threshold):
        if previous is None:
            return current <= threshold
        return previous > threshold >= current

    def _record_crossing(self, state, line, color, now, progress):
        event = {
            "type": "line_crossing",
            "line": line,
            "time_s": round(now, 4),
            "plate_id": state.plate_id,
            "batch_id": state.batch_id or "",
            "sequence": state.sequence or "",
            "expected_color": state.expected_color,
            "predicted_color": color,
            "correct": (
                int(color == state.expected_color)
                if state.expected_color
                else ""
            ),
            "progress": round(progress, 4),
        }
        self.crossing_events.append(event)

    def _update_line_crossings(self, state, color, now, progress):
        previous = state.last_progress

        if state.entry_time is None and self._crossed(
            previous,
            progress,
            ENTRY_LINE_RATIO,
        ):
            batch = self.batches[self.active_batch_id]
            sequence = len(batch["entry_ids"]) + 1
            state.batch_id = self.active_batch_id
            state.sequence = sequence
            state.expected_color = (
                EXPECTED_SEQUENCE[sequence - 1]
                if sequence <= len(EXPECTED_SEQUENCE)
                else ""
            )
            state.entry_time = now
            state.entry_color = color
            state.stage = "entered"
            batch["entry_ids"].append(state.plate_id)
            self._record_crossing(state, "entry", color, now, progress)

        if state.entry_time is not None and state.count_time is None and self._crossed(
            previous,
            progress,
            COUNTING_LINE_RATIO,
        ):
            state.count_time = now
            state.count_color = color
            state.stage = "counted"
            self._record_crossing(state, "count", color, now, progress)

        if state.entry_time is not None and state.exit_time is None and self._crossed(
            previous,
            progress,
            EXIT_LINE_RATIO,
        ):
            state.exit_candidate = True
            state.stage = "exit_candidate"

        state.last_progress = progress

    def finalize_disappearance(self, plate_id, event):
        state = self.states.get(int(plate_id))
        if state is None or event not in {"exit", "catch"}:
            return
        now = self._now()
        state.stage = "exited" if event == "exit" else "caught"
        if event == "catch":
            state.caught_time = now
        if event == "exit" and state.exit_time is None:
            state.exit_time = now
            state.exit_color = state.last_color
            batch = self.batches.get(state.batch_id)
            if batch is not None and state.plate_id not in batch["exit_ids"]:
                batch["exit_ids"].append(state.plate_id)
            self._record_crossing(
                state, "exit", state.last_color, now, state.last_progress or 0.0
            )

    def display_lines(self):
        batch = self.batches[self.active_batch_id]
        entry_ids = batch["entry_ids"]
        exit_ids = batch["exit_ids"]
        return [
            (
                f"batch={self.active_batch_id} "
                f"entry={len(entry_ids)}/3 exit={len(exit_ids)}/3"
            ),
            "expected=R > LG > O | B=next batch",
        ]

    def record(self, frame_index, inference_ms, instances, frame_shape):
        now = self._now()
        self.inference_index += 1
        self.frame_shape = tuple(frame_shape[:2])
        active_instances = []
        for item in instances:
            tracker_id = int(item["tracker_id"])
            box = tuple(float(value) for value in item["xyxy"])
            _cx, cy = self._center(box)
            progress = cy / max(1.0, float(frame_shape[0] - 1))
            mapped_plate_id = self.tracker_to_plate.get(tracker_id)
            mapped_state = self.states.get(mapped_plate_id)

            # Do not evaluate anything after EXIT.  Keep the first crossing
            # observation so it can finalize the plate, but discard later
            # observations and detections that first appear above EXIT.
            if mapped_state is not None and (
                mapped_state.exit_time is not None
                or mapped_state.caught_time is not None
            ):
                item["ignore_after_exit"] = True
                continue
            if mapped_plate_id is None and progress <= EXIT_LINE_RATIO:
                item["ignore_after_exit"] = True
                continue
            active_instances.append(item)

        duplicate_counts = self._duplicate_counts(active_instances)
        frame_colors = Counter(
            item.get("stable_color", "unknown") for item in active_instances
        )
        self.frame_rows.append({
            "session_s": round(now, 4), "frame_index": frame_index,
            "inference_index": self.inference_index, "inference_ms": round(inference_ms, 3),
            "plate_count": len(active_instances), "red_count": frame_colors["red"],
            "light_green_count": frame_colors["light_green"],
            "orange_count": frame_colors["orange"],
            "unknown_count": frame_colors["unknown"], "duplicate_pair_count": sum(duplicate_counts) // 2,
        })
        visible = set()
        for index, item in enumerate(active_instances):
            tracker_id = int(item["tracker_id"])
            box = tuple(float(value) for value in item["xyxy"])
            color = item.get("stable_color", "unknown")
            plate_id, reappeared = self._plate_for(tracker_id, box, now)
            state = self.states[plate_id]
            switched = state.observations > 0 and color != "unknown" and state.last_color != "unknown" and color != state.last_color
            state.tracker_ids.add(tracker_id)
            state.last_seen = now
            state.last_box = box
            state.observations += 1
            state.color_switches += int(switched)
            state.duplicate_frames += int(duplicate_counts[index] > 0)
            if color != "unknown":
                state.last_color = color
            visible.add(plate_id)
            cx, cy = self._center(box)
            progress = cy / max(1.0, float(frame_shape[0] - 1))
            self._update_line_crossings(
                state,
                color,
                now,
                progress,
            )
            if state.exit_time is not None:
                item["ignore_after_exit"] = True
            row = dict.fromkeys(CSV_FIELDS, "")
            row.update({
                "session_s": round(now, 4), "frame_index": frame_index,
                "inference_index": self.inference_index, "inference_ms": round(inference_ms, 3),
                "tracker_id": tracker_id, "plate_id": plate_id,
                "confidence": round(float(item.get("confidence", 0)), 5),
                "current_color": item.get("current_color", "unknown"), "stable_color": color,
                "x1": round(box[0], 2), "y1": round(box[1], 2), "x2": round(box[2], 2), "y2": round(box[3], 2),
                "center_x": round(cx, 2), "center_y": round(cy, 2),
                "width": round(box[2]-box[0], 2), "height": round(box[3]-box[1], 2),
                "duplicate_count": duplicate_counts[index], "is_reappearance": int(reappeared),
                "color_switched": int(switched),
                "progress": round(progress, 4),
                "batch_id": state.batch_id or "",
                "sequence": state.sequence or "",
                "expected_color": state.expected_color,
                "stage": state.stage,
            })
            self.rows.append(row)
            item["persistent_plate_id"] = plate_id
            item["batch_id"] = state.batch_id
            item["sequence"] = state.sequence
            item["expected_color"] = state.expected_color
            item["tracking_stage"] = state.stage
            if duplicate_counts[index] or switched or reappeared:
                self.error_points.append((int(cx), int(cy), 1.0 + duplicate_counts[index]))
        for plate_id in self.visible_last - visible:
            self.states[plate_id].disappearances += 1
        self.visible_last = visible
        self._pair_manual_events(now, visible)

    def mark_add(self, color):
        event = {"type": "ground_truth_add", "color": color, "time_s": round(self._now(), 4), "matched_plate_id": "", "latency_ms": ""}
        self.events.append(event)
        self.manual_open.append(event)
        print(f"Marked ADD {color}")

    def mark_remove(self):
        now = self._now()
        open_items = [item for item in self.manual_open if not item.get("removed")]
        target = open_items[0] if open_items else None
        event = {"type": "ground_truth_remove", "color": target["color"] if target else "unknown", "time_s": round(now, 4), "matched_plate_id": target.get("matched_plate_id", "") if target else "", "latency_ms": ""}
        self.events.append(event)
        if target:
            target["removed"] = now
            target["remove_event"] = event
        print("Marked REMOVE")

    def _pair_manual_events(self, now, visible):
        for event in self.manual_open:
            if not event["matched_plate_id"]:
                candidates = [pid for pid in visible if self.states[pid].last_color == event["color"] and self.states[pid].first_seen >= event["time_s"] - 0.25]
                if candidates:
                    pid = min(candidates, key=lambda value: self.states[value].first_seen)
                    event["matched_plate_id"] = pid
                    event["latency_ms"] = round(max(0, self.states[pid].first_seen - event["time_s"]) * 1000, 2)
            remove_event = event.get("remove_event")
            pid = event.get("matched_plate_id")
            if remove_event and pid and not remove_event["latency_ms"] and pid not in visible:
                remove_event["latency_ms"] = round(max(0, now - remove_event["time_s"]) * 1000, 2)

    def _write_csv(self, name, rows, fields):
        with (self.directory / name).open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

    def _batch_rows(self):
        rows = []
        for batch in self.batches.values():
            entry_ids = batch["entry_ids"]
            exit_ids = batch["exit_ids"]
            if not entry_ids and not exit_ids:
                continue
            missing_ids = [pid for pid in entry_ids if pid not in exit_ids]
            extra_ids = [pid for pid in exit_ids if pid not in entry_ids]
            entry_states = [self.states[pid] for pid in entry_ids]
            exit_states = [self.states[pid] for pid in exit_ids]
            entry_correct = sum(
                state.entry_color == state.expected_color
                for state in entry_states
                if state.expected_color
            )
            exit_correct = sum(
                state.exit_color == state.expected_color
                for state in exit_states
                if state.expected_color
            )
            rows.append({
                "batch_id": batch["batch_id"],
                "started_s": batch["started_s"],
                "closed_s": batch["closed_s"],
                "expected_count": len(EXPECTED_SEQUENCE),
                "entry_count": len(entry_ids),
                "exit_count": len(exit_ids),
                "entry_plate_ids": " ".join(map(str, entry_ids)),
                "exit_plate_ids": " ".join(map(str, exit_ids)),
                "missing_plate_ids": " ".join(map(str, missing_ids)),
                "extra_plate_ids": " ".join(map(str, extra_ids)),
                "entry_extra_count": max(0, len(entry_ids) - len(EXPECTED_SEQUENCE)),
                "entry_missing_count": max(0, len(EXPECTED_SEQUENCE) - len(entry_ids)),
                "order_correct": int(entry_ids == exit_ids),
                "entry_colors": " ".join(state.entry_color for state in entry_states),
                "exit_colors": " ".join(state.exit_color for state in exit_states),
                "entry_color_correct": entry_correct,
                "exit_color_correct": exit_correct,
            })
        return rows

    def finalize(self):
        if self.finalized:
            return
        self.finalized = True
        self.batches[self.active_batch_id]["closed_s"] = round(self._now(), 4)
        self._write_csv("detections.csv", self.rows, CSV_FIELDS)
        frame_fields = [
            "session_s", "frame_index", "inference_index", "inference_ms",
            "plate_count", "red_count", "light_green_count", "orange_count",
            "unknown_count", "duplicate_pair_count",
        ]
        self._write_csv("frames.csv", self.frame_rows, frame_fields)
        event_fields = ["type", "color", "time_s", "matched_plate_id", "latency_ms"]
        self._write_csv("events.csv", self.events, event_fields)
        crossing_fields = [
            "type", "line", "time_s", "plate_id", "batch_id", "sequence",
            "expected_color", "predicted_color", "correct", "progress",
        ]
        self._write_csv(
            "crossings.csv",
            self.crossing_events,
            crossing_fields,
        )
        batch_rows = self._batch_rows()
        batch_fields = [
            "batch_id", "started_s", "closed_s", "expected_count",
            "entry_count", "exit_count", "entry_plate_ids", "exit_plate_ids",
            "missing_plate_ids", "extra_plate_ids", "entry_extra_count",
            "entry_missing_count", "order_correct", "entry_colors",
            "exit_colors", "entry_color_correct", "exit_color_correct",
        ]
        self._write_csv("batches.csv", batch_rows, batch_fields)
        tracks = []
        for state in self.states.values():
            tracks.append({
                "plate_id": state.plate_id, "tracker_ids": ",".join(map(str, sorted(state.tracker_ids))),
                "first_seen_s": round(state.first_seen, 4), "last_seen_s": round(state.last_seen, 4),
                "duration_s": round(state.last_seen-state.first_seen, 4), "observations": state.observations,
                "final_color": state.last_color, "disappearances": state.disappearances,
                "reappearances": state.reappearances, "color_switches": state.color_switches,
                "duplicate_frames": state.duplicate_frames,
                "batch_id": state.batch_id or "", "sequence": state.sequence or "",
                "expected_color": state.expected_color, "stage": state.stage,
                "entry_time_s": round(state.entry_time, 4) if state.entry_time is not None else "",
                "entry_color": state.entry_color,
                "count_time_s": round(state.count_time, 4) if state.count_time is not None else "",
                "count_color": state.count_color,
                "exit_time_s": round(state.exit_time, 4) if state.exit_time is not None else "",
                "exit_color": state.exit_color,
                "final_correct": (
                    int(state.exit_color == state.expected_color)
                    if state.exit_color and state.expected_color
                    else ""
                ),
            })
        track_fields = [
            "plate_id", "tracker_ids", "first_seen_s", "last_seen_s",
            "duration_s", "observations", "final_color", "disappearances",
            "reappearances", "color_switches", "duplicate_frames", "batch_id",
            "sequence", "expected_color", "stage", "entry_time_s", "entry_color",
            "count_time_s", "count_color", "exit_time_s", "exit_color",
            "final_correct",
        ]
        self._write_csv("plates.csv", tracks, track_fields)
        colors = Counter(row["stable_color"] for row in self.rows)
        latencies = [float(e["latency_ms"]) for e in self.events if e["latency_ms"] != ""]
        summary = {
            "test_condition": {
                "meal": const.TEST_MEAL,
                "spacing": const.TEST_SPACING,
                "speed": const.TEST_SPEED,
                "planned_laps": int(const.TEST_PLANNED_LAPS),
            },
            "duration_s": round(self._now(), 3), "inference_count": self.inference_index,
            "unique_plate_count": len(self.states), "observation_color_counts": dict(colors),
            "reappearance_count": sum(s.reappearances for s in self.states.values()),
            "color_switch_count": sum(s.color_switches for s in self.states.values()),
            "duplicate_frame_count": sum(s.duplicate_frames for s in self.states.values()),
            "manual_event_count": len(self.events),
            "completed_batch_count": sum(
                row["entry_count"] == 3 and row["exit_count"] == 3
                for row in batch_rows
            ),
            "entry_crossing_count": sum(row["entry_count"] for row in batch_rows),
            "exit_crossing_count": sum(row["exit_count"] for row in batch_rows),
            "missing_at_exit_count": sum(
                len(row["missing_plate_ids"].split())
                for row in batch_rows
            ),
            "extra_at_exit_count": sum(
                len(row["extra_plate_ids"].split())
                for row in batch_rows
            ),
            "order_error_count": sum(not row["order_correct"] for row in batch_rows),
            "entry_color_correct_count": sum(
                row["entry_color_correct"] for row in batch_rows
            ),
            "exit_color_correct_count": sum(
                row["exit_color_correct"] for row in batch_rows
            ),
            "inference_mean_ms": round(float(np.mean([row["inference_ms"] for row in self.frame_rows])), 2) if self.frame_rows else None,
            "inference_p95_ms": round(float(np.percentile([row["inference_ms"] for row in self.frame_rows], 95)), 2) if self.frame_rows else None,
            "latency_mean_ms": round(float(np.mean(latencies)), 2) if latencies else None,
            "latency_max_ms": round(max(latencies), 2) if latencies else None,
            "notes": "Counts are model observations. Precision/recall require operator ground-truth events or labelled video.",
        }
        (self.directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        self._write_heatmap()
        print("Accuracy records saved:", self.directory)

    def _write_heatmap(self):
        if not self.frame_shape:
            return
        height, width = self.frame_shape
        heat = np.zeros((height, width), np.float32)
        for x, y, weight in self.error_points:
            if 0 <= x < width and 0 <= y < height:
                heat[y, x] += weight
        if not np.any(heat):
            heat[0, 0] = 0.001
        heat = cv2.GaussianBlur(heat, (0, 0), sigmaX=max(8, width / 40))
        heat = cv2.normalize(heat, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
        cv2.imwrite(str(self.directory / "error_heatmap.png"), cv2.applyColorMap(heat, cv2.COLORMAP_JET))
