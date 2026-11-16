from __future__ import annotations

import numpy as np


class Detections:
    def __init__(self, xyxy, confidence=None, class_id=None, data=None, tracker_id=None):
        self.xyxy = np.asarray(xyxy, dtype=np.float32)
        count = len(self.xyxy)
        self.confidence = _array_or_default(confidence, count, np.float32)
        self.class_id = _array_or_default(class_id, count, np.int32)
        self.data = data or {}
        self.tracker_id = (
            np.asarray(tracker_id, dtype=np.int32)
            if tracker_id is not None
            else np.arange(1, count + 1, dtype=np.int32)
        )

    def __len__(self):
        return len(self.xyxy)


class ByteTrack:
    def __init__(
        self,
        track_activation_threshold=0.0,
        lost_track_buffer=30,
        minimum_matching_threshold=0.8,
        frame_rate=30,
        minimum_consecutive_frames=1,
    ):
        self.next_id = 1
        self.previous = []
        self.track_activation_threshold = track_activation_threshold
        self.minimum_matching_threshold = minimum_matching_threshold

    def reset(self):
        self.next_id = 1
        self.previous = []

    def update_with_detections(self, detections: Detections) -> Detections:
        assigned_ids = []
        current = []

        kept_indices = []
        for index, box in enumerate(detections.xyxy):
            confidence = float(detections.confidence[index])
            if confidence + 1e-6 < self.track_activation_threshold:
                continue

            match_id = None
            match_iou = 0.0
            for previous_id, previous_box in self.previous:
                iou = _box_iou(box, previous_box)
                if iou > match_iou:
                    match_iou = iou
                    match_id = previous_id

            if match_id is None or match_iou < self.minimum_matching_threshold:
                match_id = self.next_id
                self.next_id += 1

            assigned_ids.append(match_id)
            current.append((match_id, box.copy()))
            kept_indices.append(index)

        self.previous = current
        kept_indices = np.asarray(kept_indices, dtype=np.int32)
        kept_data = {
            key: np.asarray(value)[kept_indices]
            for key, value in detections.data.items()
        }
        return Detections(
            xyxy=detections.xyxy[kept_indices],
            confidence=detections.confidence[kept_indices],
            class_id=detections.class_id[kept_indices],
            data=kept_data,
            tracker_id=np.asarray(assigned_ids, dtype=np.int32),
        )


def _array_or_default(values, count, dtype):
    if values is None:
        return np.empty((count,), dtype=dtype)
    return np.asarray(values, dtype=dtype)


def _box_iou(first, second):
    x1 = max(float(first[0]), float(second[0]))
    y1 = max(float(first[1]), float(second[1]))
    x2 = min(float(first[2]), float(second[2]))
    y2 = min(float(first[3]), float(second[3]))

    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    first_area = max(0.0, float(first[2] - first[0])) * max(0.0, float(first[3] - first[1]))
    second_area = max(0.0, float(second[2] - second[0])) * max(0.0, float(second[3] - second[1]))
    union = first_area + second_area - intersection

    if union <= 0:
        return 0.0
    return intersection / union
