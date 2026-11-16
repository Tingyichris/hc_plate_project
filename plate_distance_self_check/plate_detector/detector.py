import json
import os
import sys
import time
from collections import Counter, defaultdict, deque

import cv2
import numpy as np
from . import supervision as sv
from . import constants as const

if const.RECORDING_ENABLED:
    from .recorder import BBoxAccuracyRecorder
else:
    BBoxAccuracyRecorder = None
from .inference_sdk import InferenceConfiguration, InferenceHTTPClient


# =========================================================
# Configuration
# =========================================================

API_URL = str(const.INFERENCE_URL)

# YOLO26 Nano Instance Segmentation
MODEL_ID = str(const.INFERENCE_MODEL_ID).strip()

TARGET_CLASS = str(const.INFERENCE_TARGET_CLASS).strip()

CAMERA_ID = int(const.CAMERA_INDEX)
VIDEO_SOURCE = str(const.CAMERA_SOURCE).strip() or None
OUTPUT_VIDEO = str(const.DETECTION_OUTPUT_VIDEO).strip() or None
USE_AOI_DEFAULT = bool(const.AOI_ENABLED)
CAMERA_WIDTH = int(const.CAMERA_WIDTH)
CAMERA_HEIGHT = int(const.CAMERA_HEIGHT)
CAMERA_FPS = int(const.CAMERA_FPS)

# Temporary empty-scene false-positive capture. Each positive inference saves
# the image immediately before preprocessing and the exact model input.
CAPTURE_DETECTIONS = bool(const.DETECTION_CAPTURE_DETECTIONS)
CAPTURE_ROOT = str(const.project_path(const.DETECTION_CAPTURE_ROOT))
CAPTURE_INTERVAL_SECONDS = float(const.DETECTION_CAPTURE_INTERVAL_SECONDS)

# Model and new-track confidence threshold.
CONFIDENCE = 0.35
IOU_THRESHOLD = 0.50

# Run segmentation every N camera frames.
# Run inference on every camera frame. The effective display rate is still
# limited by camera capture and model response time.
PROCESS_EVERY_N_FRAMES = 1

AOI_FILE = str(const.project_path(const.AOI_FILE))

PREPROCESS_MODE = "clean_contrast"

DEFAULT_WARP_ENABLED = bool(const.AOI_WARP_ENABLED)
MIN_WARP_SIDE = 80
# 0 keeps the photographed trapezoid, 1 produces a fully rectangular
# bird's-eye view.  A partial correction enlarges distant plates without the
# severe blur caused by stretching a very narrow far edge to the full width.
WARP_STRENGTH = float(const.AOI_WARP_STRENGTH)

# ByteTrack
TRACK_ACTIVATION_THRESHOLD = 0.35
TRACK_LOST_BUFFER = 30
TRACK_MINIMUM_CONSECUTIVE_FRAMES = 1
# Moving conveyor plates rarely keep 80% bbox overlap between inferences.
# A 40% threshold preserves IDs while still requiring meaningful overlap.
TRACK_MINIMUM_MATCHING_THRESHOLD = 0.40

# HLS
MIN_COLOR_PIXELS = 20
MIN_COLOR_SATURATION = 25
MIN_COLOR_LIGHTNESS = 20
MAX_COLOR_LIGHTNESS = 245
MIN_CHROMATIC_COVERAGE = 0.18
MIN_ACHROMATIC_RATIO = 0.35
BLACK_LIGHTNESS_MAX = 65
WHITE_LIGHTNESS_MIN = 170
WHITE_SATURATION_MAX = 55
GRAY_LIGHTNESS_MIN = 66
GRAY_LIGHTNESS_MAX = 169
GRAY_SATURATION_MAX = 50
LAB_MAX_DISTANCE = 58.0
MIN_LAB_COLOR_SCORE = 0.12
COLOR_CLASSIFICATION_CLAHE_CLIP = 2.0
COLOR_CLASSIFICATION_CLAHE_TILE = (8, 8)
COLOR_CLASSIFICATION_SATURATION_GAIN = 1.35
LAB_COLOR_CENTERS = {
    # Calibrated from A-preprocessed production-camera anomaly captures.
    "orange": (185.0, 182.0),
    "yellow": (139.0, 190.0),
    "light_green": (118.0, 156.0),
}
HLS_HUE_RANGES = {
    "orange": [(0, 12)],
    "yellow": [(15, 26)],
    "light_green": [(28, 38)],
}
COLOR_NAMES = [
    "red",
    "orange",
    "yellow",
    "green",
    "light_green",
    "white",
    "gray",
    "black",
    "unknown",
]

# Detection input enhancement. The model and post-detection color filtering
# both use this adjusted image so reflection suppression is consistent through
# the whole pipeline. The display still uses the unmodified camera image.
# The tone curve and saturation gain are calibrated against the root reference
# image so the conveyor stays dark while plates remain vivid.
CLEAN_CONTRAST_BRIGHTNESS = -45.0
CLEAN_CONTRAST_CONTRAST = 45.0

# Colors that cannot represent a real plate in this installation. Detections
# with one of these stable colors are removed before display, counting, and
# accuracy recording. Override with a comma-separated environment variable;
# use "none" to keep every color.
EXCLUDED_PLATE_COLORS = {
    str(color).strip().lower()
    for color in const.DETECTION_EXCLUDED_COLORS
    if str(color).strip() and str(color).strip().lower() != "none"
}
COLOR_SCORE_LABELS = {
    "red": "R",
    "orange": "O",
    "yellow": "Y",
    "green": "G",
    "light_green": "LG",
    "white": "W",
    "gray": "Gy",
    "black": "K",
}

# How many color results are used for temporal voting.
COLOR_HISTORY_LENGTH = 7

# Approximate plate-edge thickness.
# Larger value analyzes a thicker portion of the plate.
RING_EROSION_RATIO = 0.18

# Mask overlay opacity.
MASK_OPACITY = 0.28

# The conveyor moves from the lower part of the image toward the upper part.
# Ratios are measured from the AOI's upper edge toward its lower edge, so the
# entrance has the larger ratio. Filled masks appear from COUNT to EXIT.
ENTRY_LINE_RATIO = 0.85
COUNTING_LINE_RATIO = 0.55
EXIT_LINE_RATIO = 0.05

# Drop duplicate detections when masks/boxes overlap too much.
OVERLAP_SMALLER_AREA_THRESHOLD = 0.45
OVERLAP_IOU_THRESHOLD = 0.35


# =========================================================
# Camera and AOI
# =========================================================

def open_video_source(source):
    if source is None:
        source = CAMERA_ID
    elif isinstance(source, str) and source.isdigit():
        source = int(source)

    if isinstance(source, int):
        capture = (
            cv2.VideoCapture(source, cv2.CAP_DSHOW)
            if sys.platform.startswith("win")
            else cv2.VideoCapture(source)
        )
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
        capture.set(cv2.CAP_PROP_FPS, CAMERA_FPS)
    else:
        capture = cv2.VideoCapture(source)

    if not capture.isOpened():
        raise RuntimeError(
            f"Cannot open video source {source!r}. "
            "Update CAMERA_INDEX/CAMERA_SOURCE in plate_detector/constants.py."
        )

    return capture


def save_aoi_polygon(polygon, frame_shape):
    frame_height, frame_width = frame_shape[:2]

    normalized = [
        [
            float(point[0]) / float(frame_width),
            float(point[1]) / float(frame_height),
        ]
        for point in polygon
    ]

    with open(AOI_FILE, "w", encoding="utf-8") as file:
        json.dump(normalized, file, indent=2)


def load_aoi_polygon(frame_shape):
    if not os.path.exists(AOI_FILE):
        return None

    with open(AOI_FILE, "r", encoding="utf-8") as file:
        normalized = json.load(file)

    frame_height, frame_width = frame_shape[:2]
    points = []

    for point in normalized:
        if not isinstance(point, list) or len(point) < 2:
            continue

        x = int(round(float(point[0]) * frame_width))
        y = int(round(float(point[1]) * frame_height))
        points.append([x, y])

    if len(points) < 3:
        return None

    polygon = np.asarray(points, dtype=np.int32)
    polygon[:, 0] = np.clip(polygon[:, 0], 0, frame_width - 1)
    polygon[:, 1] = np.clip(polygon[:, 1], 0, frame_height - 1)

    return polygon


def select_aoi_polygon(frame):
    points = []
    window_name = "Draw Conveyor AOI"

    def mouse_callback(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            points.append((x, y))
        elif event == cv2.EVENT_RBUTTONDOWN and points:
            points.pop()

    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(window_name, mouse_callback)

    while True:
        preview = frame.copy()

        if points:
            for point in points:
                cv2.circle(preview, point, 5, (0, 255, 255), -1)

            if len(points) >= 2:
                cv2.polylines(
                    preview,
                    [np.asarray(points, dtype=np.int32)],
                    isClosed=len(points) >= 3,
                    color=(0, 255, 255),
                    thickness=2,
                    lineType=cv2.LINE_AA,
                )

        cv2.putText(
            preview,
            "Left:add Right:undo Enter:save Esc:cancel",
            (15, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )

        cv2.imshow(window_name, preview)
        key = cv2.waitKey(20) & 0xFF

        if key in (10, 13) and len(points) >= 3:
            break

        if key == 27:
            cv2.destroyWindow(window_name)
            raise RuntimeError("AOI selection was cancelled.")

    cv2.destroyWindow(window_name)

    polygon = np.asarray(points, dtype=np.int32)
    save_aoi_polygon(polygon, frame.shape)

    return polygon


def load_or_select_aoi_polygon(frame):
    polygon = load_aoi_polygon(frame.shape)

    if polygon is not None:
        return polygon

    return select_aoi_polygon(frame)


def full_frame_polygon(frame):
    frame_height, frame_width = frame.shape[:2]
    return np.asarray(
        [
            [0, 0],
            [frame_width - 1, 0],
            [frame_width - 1, frame_height - 1],
            [0, frame_height - 1],
        ],
        dtype=np.int32,
    )


def crop_and_mask_aoi(frame, polygon):
    x, y, width, height = cv2.boundingRect(polygon)

    original_crop = frame[y:y + height, x:x + width].copy()

    if original_crop.size == 0:
        return None, None, 0, 0, None

    local_polygon = polygon - np.array([x, y], dtype=np.int32)

    mask = np.zeros((height, width), dtype=np.uint8)
    cv2.fillPoly(mask, [local_polygon], 255)

    masked_crop = cv2.bitwise_and(
        original_crop,
        original_crop,
        mask=mask,
    )

    return original_crop, masked_crop, x, y, local_polygon


def order_quad_points(points):
    points = np.asarray(points, dtype=np.float32)

    if points.shape != (4, 2):
        raise ValueError("Exactly four 2D points are required.")

    # The usual sum/difference shortcut can select the same vertex twice for
    # strongly skewed conveyor trapezoids.  Ordering the convex hull around
    # its centroid guarantees four distinct corners.
    hull = cv2.convexHull(points).reshape(-1, 2)
    if len(hull) != 4:
        raise ValueError("Perspective AOI must be a convex quadrilateral.")

    center = np.mean(hull, axis=0)
    angles = np.arctan2(hull[:, 1] - center[1], hull[:, 0] - center[0])
    ordered = hull[np.argsort(angles)]

    # Start at the upper-left corner. In image coordinates (Y grows down), a
    # positive cross product gives TL, TR, BR, BL order.
    start = int(np.argmin(ordered[:, 0] + ordered[:, 1]))
    ordered = np.roll(ordered, -start, axis=0)
    first_edge = ordered[1] - ordered[0]
    second_edge = ordered[2] - ordered[1]
    edge_cross = (
        first_edge[0] * second_edge[1]
        - first_edge[1] * second_edge[0]
    )
    if edge_cross < 0:
        ordered = ordered[[0, 3, 2, 1]]

    return ordered.astype(np.float32)


def build_perspective_warp(frame, polygon):
    source_polygon = polygon

    if len(source_polygon) != 4:
        hull = cv2.convexHull(source_polygon)
        perimeter = cv2.arcLength(hull, True)
        source_polygon = cv2.approxPolyDP(
            hull,
            0.03 * perimeter,
            True,
        ).reshape(-1, 2)

    if len(source_polygon) != 4:
        return None

    source = order_quad_points(source_polygon)

    top_width = np.linalg.norm(source[1] - source[0])
    bottom_width = np.linalg.norm(source[2] - source[3])

    warp_width = int(round(max(top_width, bottom_width)))

    # Area / mean width estimates the perpendicular conveyor length.  Using
    # the longest slanted side here made heavily skewed AOIs unnecessarily
    # tall and visibly distorted.
    polygon_area = abs(float(cv2.contourArea(source)))
    mean_width = max(1.0, (top_width + bottom_width) * 0.5)
    warp_height = int(round(polygon_area / mean_width))

    if warp_width < MIN_WARP_SIDE or warp_height < MIN_WARP_SIDE:
        return None

    strength = float(np.clip(WARP_STRENGTH, 0.0, 1.0))
    near_width = max(top_width, bottom_width)
    far_width = min(top_width, bottom_width)
    corrected_far_width = far_width + strength * (near_width - far_width)
    far_margin = (warp_width - corrected_far_width) * 0.5

    # The narrower edge is the distant side.  Expand it only partially while
    # leaving the near edge at its natural width.
    if top_width <= bottom_width:
        top_left = far_margin
        top_right = warp_width - 1 - far_margin
        bottom_left = 0.0
        bottom_right = warp_width - 1.0
    else:
        top_left = 0.0
        top_right = warp_width - 1.0
        bottom_left = far_margin
        bottom_right = warp_width - 1 - far_margin

    destination = np.array(
        [
            [top_left, 0],
            [top_right, 0],
            [bottom_right, warp_height - 1],
            [bottom_left, warp_height - 1],
        ],
        dtype=np.float32,
    )

    matrix = cv2.getPerspectiveTransform(
        source,
        destination,
    )

    inverse_matrix = cv2.getPerspectiveTransform(
        destination,
        source,
    )

    warped_original = cv2.warpPerspective(
        frame,
        matrix,
        (warp_width, warp_height),
        flags=cv2.INTER_LINEAR,
    )

    # Do not let pixels outside the selected conveyor AOI leak into the model
    # input through the unused margins of a partial perspective correction.
    destination_mask = np.zeros((warp_height, warp_width), dtype=np.uint8)
    cv2.fillConvexPoly(
        destination_mask,
        np.rint(destination).astype(np.int32),
        255,
    )
    warped_original = cv2.bitwise_and(
        warped_original,
        warped_original,
        mask=destination_mask,
    )

    return warped_original, inverse_matrix


def prepare_aoi_view(frame, polygon, warp_enabled):
    if warp_enabled:
        warped = build_perspective_warp(frame, polygon)

        if warped is not None:
            warped_original, inverse_matrix = warped
            return warped_original, warped_original, 0, 0, inverse_matrix, True

    original_aoi, masked_aoi, aoi_x, aoi_y, _local_polygon = (
        crop_and_mask_aoi(frame, polygon)
    )

    return original_aoi, masked_aoi, aoi_x, aoi_y, None, False


def preprocess_aoi_for_inference(masked_aoi):
    if PREPROCESS_MODE == "clean_contrast":
        # Apply brightness first, then contrast. Work in BGR so no hue or
        # saturation operation
        # can create patchy false colors on a smooth plate surface.
        valid_pixels = np.any(masked_aoi != 0, axis=2)
        darkened = np.clip(
            masked_aoi.astype(np.float32) + CLEAN_CONTRAST_BRIGHTNESS,
            0,
            255,
        )
        contrast = CLEAN_CONTRAST_CONTRAST
        contrast_factor = (
            259.0 * (contrast + 255.0)
        ) / (
            255.0 * (259.0 - contrast)
        )
        enhanced = np.clip(
            contrast_factor * (darkened - 128.0) + 128.0,
            0,
            255,
        ).astype(np.uint8)
        enhanced[~valid_pixels] = 0
        return enhanced

    # Defensive fallback if PREPROCESS_MODE is changed incorrectly.
    return masked_aoi


def draw_aoi_overlay(frame, polygon):
    overlay = frame.copy()

    cv2.fillPoly(
        overlay,
        [polygon],
        (40, 150, 70),
    )

    cv2.addWeighted(
        overlay,
        0.16,
        frame,
        0.84,
        0,
        frame,
    )


def interpolate_aoi_cross_section(polygon, ratio):
    """Return a cross-section for a four-point conveyor AOI."""
    points = np.asarray(polygon, dtype=np.float32)

    if len(points) != 4:
        x, y, width, height = cv2.boundingRect(points.astype(np.int32))
        line_y = y + int(round(height * ratio))
        return np.asarray([[x, line_y], [x + width, line_y]], dtype=np.int32)

    # AOI selection order is upper-left, lower-left, lower-right,
    # upper-right. Interpolate both conveyor edges by the same ratio.
    left = points[0] + (points[1] - points[0]) * ratio
    right = points[3] + (points[2] - points[3]) * ratio
    return np.rint([left, right]).astype(np.int32)


def counting_zone_polygon(aoi_polygon):
    counting_line = interpolate_aoi_cross_section(
        aoi_polygon,
        COUNTING_LINE_RATIO,
    )
    exit_line = interpolate_aoi_cross_section(
        aoi_polygon,
        EXIT_LINE_RATIO,
    )
    return np.asarray(
        [counting_line[0], exit_line[0], exit_line[1], counting_line[1]],
        dtype=np.int32,
    )


def draw_tracking_guides(frame, aoi_polygon):
    guides = [
        ("ENTRY", ENTRY_LINE_RATIO, (255, 210, 60)),
        ("COUNT", COUNTING_LINE_RATIO, (0, 255, 255)),
        ("EXIT", EXIT_LINE_RATIO, (80, 100, 255)),
    ]

    cv2.polylines(
        frame,
        [np.asarray(aoi_polygon, dtype=np.int32)],
        True,
        (80, 230, 100),
        2,
        cv2.LINE_AA,
    )

    for label, ratio, color in guides:
        line = interpolate_aoi_cross_section(aoi_polygon, ratio)
        cv2.line(
            frame,
            tuple(line[0]),
            tuple(line[1]),
            color,
            2,
            cv2.LINE_AA,
        )
        label_x = int((line[0, 0] + line[1, 0]) / 2)
        label_y = int((line[0, 1] + line[1, 1]) / 2)
        cv2.putText(
            frame,
            label,
            (label_x + 6, label_y - 6),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.48,
            color,
            1,
            cv2.LINE_AA,
        )

# =========================================================
# Inference
# =========================================================

def normalize_result(result):
    if isinstance(result, list):
        if not result:
            return {"predictions": []}
        result = result[0]

    if not isinstance(result, dict):
        return {"predictions": []}

    return result


def run_inference(client, aoi_image):
    result = normalize_result(
        client.infer(
            aoi_image,
            model_id=MODEL_ID,
        )
    )
    # Enforce the threshold locally as well. Do not rely solely on the
    # inference server honoring the requested confidence configuration.
    result["predictions"] = [
        prediction
        for prediction in result.get("predictions", [])
        if float(prediction.get("confidence", 0.0)) >= CONFIDENCE
    ]
    return result


# =========================================================
# Polygon and Supervision conversion
# =========================================================

def prediction_to_polygon(prediction, image_width, image_height):
    points = prediction.get("points", [])

    if len(points) < 3:
        return None

    polygon = np.array(
        [
            [
                int(round(point["x"])),
                int(round(point["y"])),
            ]
            for point in points
        ],
        dtype=np.int32,
    )

    polygon[:, 0] = np.clip(
        polygon[:, 0],
        0,
        image_width - 1,
    )
    polygon[:, 1] = np.clip(
        polygon[:, 1],
        0,
        image_height - 1,
    )

    return polygon


def describe_predictions(result):
    predictions = result.get("predictions", [])
    classes = Counter(
        str(prediction.get("class", "unknown"))
        for prediction in predictions
    )
    best_confidence = max(
        (
            float(prediction.get("confidence", 0.0))
            for prediction in predictions
        ),
        default=0.0,
    )
    return predictions, classes, best_confidence


def save_detection_capture(
    session_dir,
    frame_index,
    before_preprocess,
    model_input,
    predictions,
    tracked_instances,
    preprocess_mode,
    warp_active,
    use_aoi,
):
    """Save input images, tracked mask overlay, and JSONL metadata."""
    timestamp = time.time()
    timestamp_text = time.strftime(
        "%Y%m%d_%H%M%S",
        time.localtime(timestamp),
    )
    milliseconds = int((timestamp % 1.0) * 1000)
    sample_id = f"{timestamp_text}_{milliseconds:03d}_f{frame_index:06d}"
    original_name = f"{sample_id}_original_raw.png"
    original_bbox_name = f"{sample_id}_original_bbox.png"
    processed_name = f"{sample_id}_processed.png"
    tracked_name = f"{sample_id}_tracked_mask.png"
    original_path = os.path.join(session_dir, original_name)
    original_bbox_path = os.path.join(session_dir, original_bbox_name)
    processed_path = os.path.join(session_dir, processed_name)
    tracked_path = os.path.join(session_dir, tracked_name)

    original_bbox_image = before_preprocess.copy()
    tracked_image = model_input.copy()
    mask_overlay = tracked_image.copy()
    for instance in tracked_instances:
        polygon = np.rint(instance["polygon"]).astype(np.int32)
        mask_color = color_bgr(instance.get("stable_color", "unknown"))
        cv2.fillPoly(mask_overlay, [polygon], mask_color)
    cv2.addWeighted(
        mask_overlay,
        MASK_OPACITY,
        tracked_image,
        1.0 - MASK_OPACITY,
        0,
        tracked_image,
    )
    for instance in tracked_instances:
        polygon = np.rint(instance["polygon"]).astype(np.int32)
        mask_color = color_bgr(instance.get("stable_color", "unknown"))
        box_x1, box_y1, box_x2, box_y2 = (
            np.rint(instance["xyxy"]).astype(int).tolist()
        )
        cv2.polylines(
            tracked_image,
            [polygon],
            True,
            mask_color,
            2,
            cv2.LINE_AA,
        )
        tracker_id = instance.get("tracker_id")
        plate_id = instance.get("persistent_plate_id")
        identity = (
            f"P{plate_id}/T{tracker_id}"
            if plate_id is not None
            else f"T{tracker_id}"
        )
        label = (
            f"{identity} {instance.get('confidence', 0.0):.2f} "
            f"{instance.get('stable_color', 'unknown')}"
        )
        label_x = int(np.min(polygon[:, 0]))
        label_y = max(18, int(np.min(polygon[:, 1])) - 6)
        cv2.rectangle(
            original_bbox_image,
            (box_x1, box_y1),
            (box_x2, box_y2),
            mask_color,
            3,
            cv2.LINE_AA,
        )
        cv2.putText(
            original_bbox_image,
            label,
            (max(0, box_x1), max(18, box_y1 - 6)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        cv2.putText(
            tracked_image,
            label,
            (label_x, label_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )

    original_saved = cv2.imwrite(original_path, before_preprocess)
    original_bbox_saved = cv2.imwrite(
        original_bbox_path,
        original_bbox_image,
    )
    processed_saved = cv2.imwrite(processed_path, model_input)
    tracked_saved = cv2.imwrite(tracked_path, tracked_image)
    if (
        not original_saved
        or not original_bbox_saved
        or not processed_saved
        or not tracked_saved
    ):
        raise RuntimeError(f"Could not save detection capture {sample_id}")

    tracked_records = []
    for instance in tracked_instances:
        tracked_records.append(
            {
                "tracker_id": instance.get("tracker_id"),
                "plate_id": instance.get("persistent_plate_id"),
                "confidence": float(instance.get("confidence", 0.0)),
                "current_color": instance.get("current_color"),
                "stable_color": instance.get("stable_color"),
                "xyxy": np.asarray(instance["xyxy"]).astype(float).tolist(),
                "polygon": np.asarray(instance["polygon"]).astype(int).tolist(),
            }
        )

    record = {
        "sample_id": sample_id,
        "timestamp": timestamp,
        "frame_index": frame_index,
        "original_raw": original_name,
        "original_bbox": original_bbox_name,
        "processed": processed_name,
        "tracked_mask": tracked_name,
        "preprocess_mode": preprocess_mode,
        "warp_active": bool(warp_active),
        "use_aoi": bool(use_aoi),
        "prediction_count": len(predictions),
        "predictions": predictions,
        "tracked_count": len(tracked_records),
        "tracked_instances": tracked_records,
    }
    metadata_path = os.path.join(session_dir, "detections.jsonl")
    with open(metadata_path, "a", encoding="utf-8") as metadata_file:
        metadata_file.write(json.dumps(record, ensure_ascii=False) + "\n")

    return sample_id


def predictions_to_detections(result, aoi_width, aoi_height):
    """
    Convert Roboflow object-detection output into sv.Detections.

    The bbox model does not return segmentation points. When points are absent,
    we use the detection box as a rectangular polygon so the existing color,
    ordering, tracking, and drawing logic can stay shared.
    """
    boxes = []
    confidences = []
    class_ids = []
    class_names = []
    source_indices = []
    polygons = []

    for prediction in result.get("predictions", []):
        prediction_class = str(prediction.get("class", ""))

        if TARGET_CLASS and prediction_class != TARGET_CLASS:
            continue

        confidence = float(prediction.get("confidence", 0.0))

        # Second defensive gate for callers/tests that pass a result directly
        # instead of going through run_inference().
        if confidence < CONFIDENCE:
            continue

        center_x = float(prediction["x"])
        center_y = float(prediction["y"])
        box_width = float(prediction["width"])
        box_height = float(prediction["height"])

        x1 = center_x - box_width / 2.0
        y1 = center_y - box_height / 2.0
        x2 = center_x + box_width / 2.0
        y2 = center_y + box_height / 2.0

        x1 = np.clip(x1, 0, aoi_width - 1)
        y1 = np.clip(y1, 0, aoi_height - 1)
        x2 = np.clip(x2, 0, aoi_width - 1)
        y2 = np.clip(y2, 0, aoi_height - 1)

        polygon = prediction_to_polygon(
            prediction,
            image_width=aoi_width,
            image_height=aoi_height,
        )

        if polygon is None:
            polygon = np.asarray(
                [
                    [int(round(x1)), int(round(y1))],
                    [int(round(x2)), int(round(y1))],
                    [int(round(x2)), int(round(y2))],
                    [int(round(x1)), int(round(y2))],
                ],
                dtype=np.int32,
            )

        source_index = len(polygons)
        polygons.append(polygon)

        boxes.append([x1, y1, x2, y2])
        confidences.append(confidence)
        class_ids.append(int(prediction.get("class_id", 0)))
        class_names.append(prediction_class)
        source_indices.append(source_index)

    if not boxes:
        detections = sv.Detections(
            xyxy=np.empty((0, 4), dtype=np.float32),
            confidence=np.empty((0,), dtype=np.float32),
            class_id=np.empty((0,), dtype=np.int32),
            data={
                "class_name": np.empty((0,), dtype=object),
                "source_index": np.empty((0,), dtype=np.int32),
            },
        )
        return detections, polygons

    detections = sv.Detections(
        xyxy=np.asarray(boxes, dtype=np.float32),
        confidence=np.asarray(confidences, dtype=np.float32),
        class_id=np.asarray(class_ids, dtype=np.int32),
        data={
            "class_name": np.asarray(class_names, dtype=object),
            "source_index": np.asarray(source_indices, dtype=np.int32),
        },
    )

    return detections, polygons


# =========================================================
# HLS plate-edge classification
# =========================================================

def create_plate_ring_mask(image_shape, polygon):
    """
    原始 polygon 用來顯示模型結果。

    HLS 盤緣則使用 polygon 的 convex hull 建立較平滑的外框，
    最後與原始 segmentation mask 交集，只保留模型認為是盤子的像素。
    """
    image_height, image_width = image_shape[:2]

    # 原始 segmentation mask
    segmentation_mask = np.zeros(
        (image_height, image_width),
        dtype=np.uint8,
    )

    cv2.fillPoly(
        segmentation_mask,
        [polygon],
        255,
    )

    # 盤子近似凸形，填補被盤上物體造成的內凹輪廓
    hull = cv2.convexHull(polygon)

    hull_mask = np.zeros(
        (image_height, image_width),
        dtype=np.uint8,
    )

    cv2.fillPoly(
        hull_mask,
        [hull],
        255,
    )

    _, _, width, height = cv2.boundingRect(hull)

    plate_size = max(1, min(width, height))

    erosion_pixels = max(
        2,
        int(round(plate_size * RING_EROSION_RATIO)),
    )

    kernel_size = max(
        3,
        erosion_pixels * 2 + 1,
    )

    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (kernel_size, kernel_size),
    )

    eroded_hull = cv2.erode(
        hull_mask,
        kernel,
        iterations=1,
    )

    # 凸包的外側環形區域
    geometric_ring = cv2.subtract(
        hull_mask,
        eroded_hull,
    )

    # 只取模型認為屬於盤子的可見像素，
    # 避免凸包補出的遮擋區域影響 HLS。
    ring_mask = cv2.bitwise_and(
        geometric_ring,
        segmentation_mask,
    )

    return segmentation_mask, ring_mask



def enhance_for_color_classification(image, mask):
    mask_pixels = mask > 0

    if not np.any(mask_pixels):
        return image

    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    l_channel, a_channel, b_channel = cv2.split(lab)
    clahe = cv2.createCLAHE(
        clipLimit=COLOR_CLASSIFICATION_CLAHE_CLIP,
        tileGridSize=COLOR_CLASSIFICATION_CLAHE_TILE,
    )
    enhanced_l = clahe.apply(l_channel)
    enhanced_lab = cv2.merge((enhanced_l, a_channel, b_channel))
    enhanced_bgr = cv2.cvtColor(enhanced_lab, cv2.COLOR_LAB2BGR)

    classification_image = image.copy()
    classification_image[mask_pixels] = enhanced_bgr[mask_pixels]

    hls = cv2.cvtColor(classification_image, cv2.COLOR_BGR2HLS)
    saturation = hls[:, :, 2].astype(np.float32)
    saturation[mask_pixels] = np.clip(
        saturation[mask_pixels] * COLOR_CLASSIFICATION_SATURATION_GAIN,
        0,
        255,
    )
    hls[:, :, 2] = saturation.astype(np.uint8)
    boosted_bgr = cv2.cvtColor(hls, cv2.COLOR_HLS2BGR)
    classification_image[mask_pixels] = boosted_bgr[mask_pixels]

    return classification_image


def select_color_sample_mask(segmentation_mask, ring_mask):
    ring_pixels = ring_mask > 0

    if int(np.count_nonzero(ring_pixels)) >= MIN_COLOR_PIXELS:
        return ring_pixels

    return segmentation_mask > 0


def score_lab_color_votes(lab_image, color_pixels):
    lab_scores = {
        color_name: 0.0
        for color_name in LAB_COLOR_CENTERS
    }

    color_count = int(np.count_nonzero(color_pixels))

    if color_count < MIN_COLOR_PIXELS:
        return lab_scores

    a_values = lab_image[:, :, 1][color_pixels].astype(np.float32)
    b_values = lab_image[:, :, 2][color_pixels].astype(np.float32)

    color_names = list(LAB_COLOR_CENTERS)
    distances = np.stack(
        [
            np.hypot(a_values - center_a, b_values - center_b)
            for center_a, center_b in LAB_COLOR_CENTERS.values()
        ],
        axis=1,
    )
    nearest = np.argmin(distances, axis=1)
    nearest_distances = distances[
        np.arange(color_count),
        nearest,
    ]
    confident_votes = nearest_distances <= (
        LAB_MAX_DISTANCE * (1.0 - MIN_LAB_COLOR_SCORE)
    )

    if not np.any(confident_votes):
        return lab_scores

    for color_index, color_name in enumerate(color_names):
        lab_scores[color_name] = float(
            np.count_nonzero(
                confident_votes & (nearest == color_index)
            )
        ) / color_count

    return lab_scores


def score_hls_hue_votes(hls_image, color_pixels):
    hue_scores = {
        color_name: 0.0
        for color_name in LAB_COLOR_CENTERS
    }
    color_count = int(np.count_nonzero(color_pixels))

    if color_count < MIN_COLOR_PIXELS:
        return hue_scores

    hue = hls_image[:, :, 0]

    for color_name, ranges in HLS_HUE_RANGES.items():
        color_match = np.zeros(hue.shape, dtype=bool)

        for hue_min, hue_max in ranges:
            color_match |= (
                (hue >= hue_min)
                & (hue <= hue_max)
            )

        hue_scores[color_name] = float(
            np.count_nonzero(color_pixels & color_match)
        ) / color_count

    return hue_scores


def combine_chromatic_scores(lab_scores, hue_scores):
    return {
        color_name: (
            0.25 * lab_scores[color_name]
            + 0.75 * hue_scores[color_name]
        )
        for color_name in LAB_COLOR_CENTERS
    }


def classify_plate_hls(aoi_image, polygon):
    """
    OpenCV HLS ranges:
      H: 0..180
      L: 0..255
      S: 0..255

    These ranges are initial calibration values and should be adjusted
    using images from the production camera.
    """
    segmentation_mask, ring_mask = create_plate_ring_mask(
        aoi_image.shape,
        polygon,
    )

    original_hls = cv2.cvtColor(
        aoi_image,
        cv2.COLOR_BGR2HLS,
    )
    original_lightness = original_hls[:, :, 1]
    original_saturation = original_hls[:, :, 2]

    classification_image = enhance_for_color_classification(
        aoi_image,
        segmentation_mask,
    )

    hls = cv2.cvtColor(classification_image, cv2.COLOR_BGR2HLS)

    sample_pixels = select_color_sample_mask(
        segmentation_mask,
        ring_mask,
    )
    sample_count = int(np.count_nonzero(sample_pixels))

    if sample_count < MIN_COLOR_PIXELS:
        return "unknown", {
            color_name: 0.0
            for color_name in COLOR_NAMES
            if color_name != "unknown"
        }, ring_mask

    black_pixels = sample_pixels & (original_lightness <= BLACK_LIGHTNESS_MAX)
    white_pixels = (
        sample_pixels
        & (original_lightness >= WHITE_LIGHTNESS_MIN)
        & (original_saturation <= WHITE_SATURATION_MAX)
    )
    gray_pixels = (
        sample_pixels
        & (original_lightness >= GRAY_LIGHTNESS_MIN)
        & (original_lightness <= GRAY_LIGHTNESS_MAX)
        & (original_saturation <= GRAY_SATURATION_MAX)
    )

    usable_lightness = (
        (original_lightness >= MIN_COLOR_LIGHTNESS)
        & (original_lightness <= MAX_COLOR_LIGHTNESS)
    )
    color_pixels = (
        sample_pixels
        & usable_lightness
        & (original_saturation >= MIN_COLOR_SATURATION)
    )
    color_count = int(np.count_nonzero(color_pixels))
    chromatic_coverage = float(color_count) / sample_count

    lab = cv2.cvtColor(classification_image, cv2.COLOR_BGR2LAB)
    lab_scores = score_lab_color_votes(lab, color_pixels)
    hue_scores = score_hls_hue_votes(original_hls, color_pixels)
    chromatic_scores = combine_chromatic_scores(
        lab_scores,
        hue_scores,
    )

    ratios = {
        **chromatic_scores,
        "white": float(np.count_nonzero(white_pixels)) / sample_count,
        "gray": float(np.count_nonzero(gray_pixels)) / sample_count,
        "black": float(np.count_nonzero(black_pixels)) / sample_count,
    }

    candidate_thresholds = {
        color_name: MIN_LAB_COLOR_SCORE
        for color_name in chromatic_scores
    }
    candidate_thresholds.update(
        {
            "black": MIN_ACHROMATIC_RATIO,
            "white": MIN_ACHROMATIC_RATIO,
            "gray": MIN_ACHROMATIC_RATIO,
        }
    )
    best_color = max(
        candidate_thresholds,
        key=lambda color_name: ratios.get(color_name, 0.0),
    )

    if ratios.get(best_color, 0.0) < candidate_thresholds[best_color]:
        best_color = "unknown"

    return best_color, ratios, ring_mask


def stable_color_from_history(history):
    known_colors = [
        color
        for color in history
        if color != "unknown"
    ]

    if not known_colors:
        return "unknown"

    counts = Counter(known_colors)
    return counts.most_common(1)[0][0]


def color_bgr(color_name):
    return {
        "red": (0, 0, 255),
        "orange": (0, 140, 255),
        "yellow": (0, 255, 255),
        "green": (0, 220, 0),
        "light_green": (120, 255, 120),
        "white": (245, 245, 245),
        "gray": (150, 150, 150),
        "black": (35, 35, 35),
        "unknown": (0, 255, 255),
    }.get(color_name, (0, 255, 255))


def format_color_counts(color_counts):
    return (
        f"R={color_counts['red']} "
        f"O={color_counts['orange']} "
        f"Y={color_counts['yellow']} "
        f"G={color_counts['green']} "
        f"LG={color_counts['light_green']} "
        f"W={color_counts['white']} "
        f"Gy={color_counts['gray']} "
        f"K={color_counts['black']} "
        f"U={color_counts['unknown']}"
    )


def format_color_scores(ratios):
    top_scores = sorted(
        (
            (color_name, ratios.get(color_name, 0.0))
            for color_name in COLOR_SCORE_LABELS
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    return " ".join(
        f"{COLOR_SCORE_LABELS[color_name]}:{score:.2f}"
        for color_name, score in top_scores[:2]
    )


def polygon_mask(image_shape, polygon):
    mask = np.zeros(
        image_shape[:2],
        dtype=np.uint8,
    )

    cv2.fillPoly(
        mask,
        [polygon],
        255,
    )

    return mask


def box_iou(first, second):
    x1 = max(float(first[0]), float(second[0]))
    y1 = max(float(first[1]), float(second[1]))
    x2 = min(float(first[2]), float(second[2]))
    y2 = min(float(first[3]), float(second[3]))

    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    first_area = max(0.0, float(first[2] - first[0])) * max(
        0.0,
        float(first[3] - first[1]),
    )
    second_area = max(0.0, float(second[2] - second[0])) * max(
        0.0,
        float(second[3] - second[1]),
    )
    union = first_area + second_area - intersection

    if union <= 0:
        return 0.0

    return intersection / union


def assign_display_order(instances):
    ordered = sorted(
        instances,
        key=lambda item: (
            item.get("track_order_y", 0.0),
            item.get("track_order_x", 0.0),
            -item.get("confidence", 0.0),
        ),
    )

    for display_order, instance in enumerate(ordered, start=1):
        instance["display_order"] = display_order

    return ordered


def filter_overlapping_instances(instances, image_shape):
    if not instances:
        return []

    candidates = []

    for instance in instances:
        mask = polygon_mask(
            image_shape,
            instance["polygon"],
        )

        area = int(cv2.countNonZero(mask))

        if area <= 0:
            continue

        candidate = dict(instance)
        candidate["area"] = area
        x1, y1, x2, y2 = candidate["xyxy"]
        candidate["track_order_x"] = float(x1 + x2) / 2.0
        candidate["track_order_y"] = float(y1 + y2) / 2.0
        candidate["_mask"] = mask
        candidates.append(candidate)

    candidates.sort(
        key=lambda item: (
            item["confidence"],
            item["area"],
        ),
        reverse=True,
    )

    kept = []

    for candidate in candidates:
        should_keep = True

        for kept_instance in kept:
            intersection = cv2.bitwise_and(
                candidate["_mask"],
                kept_instance["_mask"],
            )

            intersection_area = int(
                cv2.countNonZero(intersection)
            )

            smaller_area = max(
                1,
                min(
                    candidate["area"],
                    kept_instance["area"],
                ),
            )

            overlap_ratio = (
                intersection_area / float(smaller_area)
            )

            iou_ratio = box_iou(
                candidate["xyxy"],
                kept_instance["xyxy"],
            )

            if (
                overlap_ratio >= OVERLAP_SMALLER_AREA_THRESHOLD
                or iou_ratio >= OVERLAP_IOU_THRESHOLD
            ):
                should_keep = False
                break

        if should_keep:
            kept.append(candidate)

    for instance in kept:
        instance.pop("_mask", None)

    return assign_display_order(kept)


def draw_summary_panel(
    frame,
    tracked_instances,
    inference_ms,
    preprocess_mode,
    warp_active,
    use_aoi,
    recorder_lines=None,
):
    visible_instances = [
        instance
        for instance in tracked_instances
        if not instance.get("ignore_after_exit")
    ]

    header_lines = [
        (
            f"plates={len(visible_instances)} | "
            f"inference={inference_ms:.0f} ms | "
            f"mode={preprocess_mode} | "
            f"warp={'on' if warp_active else 'off'} | "
            f"aoi={'on' if use_aoi else 'full'}"
        ),
        "Q=quit B=next batch A=AOI R=redraw W=warp S=save",
    ]
    header_lines.extend(recorder_lines or [])

    plate_items = []
    for instance in visible_instances[:8]:
        plate_id = instance.get("persistent_plate_id")
        tracker_id = instance["tracker_id"]
        identity = (
            f"P{plate_id}/T{tracker_id}"
            if plate_id is not None
            else f"T{tracker_id}"
        )
        expected = instance.get("expected_color") or "-"
        plate_items.append(
            (
                f"{identity} GT={expected}:",
                color_bgr(instance["stable_color"]),
            )
        )

    footer_lines = []
    if len(visible_instances) > 8:
        footer_lines.append(
            f"... {len(visible_instances) - 8} more"
        )

    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.48
    thickness = 1
    padding = 10
    line_gap = 8
    circle_radius = 7
    circle_gap = 10

    text_lines = header_lines + footer_lines
    text_sizes = [
        cv2.getTextSize(line, font, font_scale, thickness)[0]
        for line in text_lines
    ]
    item_text_sizes = [
        cv2.getTextSize(prefix, font, font_scale, thickness)[0]
        for prefix, _color in plate_items
    ]

    max_text_width = max(
        [width for width, _height in text_sizes] + [0]
    )
    max_item_width = max(
        [
            width + circle_gap + circle_radius * 2
            for width, _height in item_text_sizes
        ] + [0]
    )
    max_text_height = max(
        [height for _width, height in text_sizes + item_text_sizes] + [0]
    )
    panel_width = max(max_text_width, max_item_width) + padding * 2
    line_height = max(max_text_height, circle_radius * 2)
    line_count = len(header_lines) + len(plate_items) + len(footer_lines)
    panel_height = (
        line_count * line_height
        + max(0, line_count - 1) * line_gap
        + padding * 2
    )

    x1 = 12
    y2 = frame.shape[0] - 12
    y1 = max(12, y2 - panel_height)
    x2 = min(frame.shape[1] - 1, x1 + panel_width)

    overlay = frame.copy()
    cv2.rectangle(
        overlay,
        (x1, y1),
        (x2, y2),
        (20, 20, 20),
        -1,
    )
    cv2.addWeighted(
        overlay,
        0.72,
        frame,
        0.28,
        0,
        frame,
    )
    cv2.rectangle(
        frame,
        (x1, y1),
        (x2, y2),
        (255, 255, 255),
        1,
    )

    y = y1 + padding + line_height

    for line in header_lines:
        cv2.putText(
            frame,
            line,
            (x1 + padding, y),
            font,
            font_scale,
            (255, 255, 255),
            thickness,
            cv2.LINE_AA,
        )
        y += line_height + line_gap

    for prefix, circle_color in plate_items:
        cv2.putText(
            frame,
            prefix,
            (x1 + padding, y),
            font,
            font_scale,
            (255, 255, 255),
            thickness,
            cv2.LINE_AA,
        )
        prefix_size = cv2.getTextSize(
            prefix,
            font,
            font_scale,
            thickness,
        )[0]
        circle_x = x1 + padding + prefix_size[0] + circle_gap + circle_radius
        circle_y = y - line_height // 2 + 2
        cv2.circle(
            frame,
            (circle_x, circle_y),
            circle_radius,
            circle_color,
            -1,
            cv2.LINE_AA,
        )
        cv2.circle(
            frame,
            (circle_x, circle_y),
            circle_radius,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
        y += line_height + line_gap

    for line in footer_lines:
        cv2.putText(
            frame,
            line,
            (x1 + padding, y),
            font,
            font_scale,
            (255, 255, 255),
            thickness,
            cv2.LINE_AA,
        )
        y += line_height + line_gap


# =========================================================
# Tracking and visualization
# =========================================================

def process_predictions(
    result,
    original_aoi,
    tracker,
    color_history,
):
    aoi_height, aoi_width = original_aoi.shape[:2]

    detections, polygons = predictions_to_detections(
        result=result,
        aoi_width=aoi_width,
        aoi_height=aoi_height,
    )

    tracked_detections = tracker.update_with_detections(
        detections
    )

    tracked_instances = []

    if len(tracked_detections) == 0:
        return tracked_instances

    source_indices = tracked_detections.data.get(
        "source_index",
        np.empty((0,), dtype=np.int32),
    )

    for index in range(len(tracked_detections)):
        if index >= len(source_indices):
            continue

        source_index = int(source_indices[index])

        if source_index < 0 or source_index >= len(polygons):
            continue

        polygon = polygons[source_index]

        tracker_id = int(
            tracked_detections.tracker_id[index]
        )

        confidence = float(
            tracked_detections.confidence[index]
        )

        # Final application-side gate before an object can be displayed,
        # recorded, or assigned a permanent plate ID.
        if confidence + 1e-6 < TRACK_ACTIVATION_THRESHOLD:
            continue

        xyxy = tracked_detections.xyxy[index].copy()

        current_color, ratios, ring_mask = classify_plate_hls(
            original_aoi,
            polygon,
        )

        color_history[tracker_id].append(
            current_color
        )

        stable_color = stable_color_from_history(
            color_history[tracker_id]
        )

        if stable_color.lower() in EXCLUDED_PLATE_COLORS:
            continue

        tracked_instances.append(
            {
                "tracker_id": tracker_id,
                "confidence": confidence,
                "xyxy": xyxy,
                "polygon": polygon,
                "current_color": current_color,
                "stable_color": stable_color,
                "ratios": ratios,
                "ring_mask": ring_mask,
            }
        )

    return filter_overlapping_instances(
        tracked_instances,
        original_aoi.shape,
    )


def project_polygon_to_frame(
    polygon,
    aoi_x,
    aoi_y,
    frame_shape,
    inverse_matrix=None,
):
    projected = polygon.astype(np.float32)

    if inverse_matrix is not None:
        projected = cv2.perspectiveTransform(
            projected.reshape(-1, 1, 2),
            inverse_matrix,
        ).reshape(-1, 2)
    else:
        projected[:, 0] += aoi_x
        projected[:, 1] += aoi_y

    projected = np.rint(projected).astype(np.int32)
    projected[:, 0] = np.clip(projected[:, 0], 0, frame_shape[1] - 1)
    projected[:, 1] = np.clip(projected[:, 1], 0, frame_shape[0] - 1)

    return projected


def project_box_to_frame(
    xyxy,
    aoi_x,
    aoi_y,
    frame_shape,
    inverse_matrix=None,
):
    x1, y1, x2, y2 = xyxy
    corners = np.asarray(
        [
            [x1, y1],
            [x2, y1],
            [x2, y2],
            [x1, y2],
        ],
        dtype=np.float32,
    )
    projected = project_polygon_to_frame(
        corners,
        aoi_x,
        aoi_y,
        frame_shape,
        inverse_matrix,
    )

    box_x1 = int(np.min(projected[:, 0]))
    box_y1 = int(np.min(projected[:, 1]))
    box_x2 = int(np.max(projected[:, 0]))
    box_y2 = int(np.max(projected[:, 1]))

    return box_x1, box_y1, box_x2, box_y2


def draw_tracked_instances(
    frame,
    tracked_instances,
    aoi_x,
    aoi_y,
    aoi_polygon,
    inverse_matrix=None,
):
    # Keep segmentation polygons internally for color classification and
    # tracking, but present detections as simple rectangular bounding boxes.
    for instance in tracked_instances:
        if instance.get("ignore_after_exit"):
            continue
        stable_color = instance["stable_color"]

        color = color_bgr(stable_color)
        box_x1, box_y1, box_x2, box_y2 = project_box_to_frame(
            instance["xyxy"],
            aoi_x,
            aoi_y,
            frame.shape,
            inverse_matrix,
        )

        cv2.rectangle(
            frame,
            (box_x1, box_y1),
            (box_x2, box_y2),
            color,
            2,
            cv2.LINE_AA,
        )

        label_x = box_x1
        label_y = box_y1

        plate_id = instance.get("persistent_plate_id")
        tracker_id = instance["tracker_id"]
        label = (
            f"P{plate_id}/T{tracker_id}"
            if plate_id is not None
            else f"T{tracker_id}"
        )
        expected = instance.get("expected_color")
        if expected:
            label += f" GT={expected}"

        cv2.putText(
            frame,
            label,
            (label_x, max(22, label_y - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.48,
            color,
            2,
            cv2.LINE_AA,
        )


# =========================================================
# Main
# =========================================================

def main():
    api_key = str(const.INFERENCE_API_KEY).strip()

    if not api_key:
        raise RuntimeError(
            "INFERENCE_API_KEY is empty in plate_detector/constants.py."
        )

    client = InferenceHTTPClient(
        api_url=API_URL,
        api_key=api_key,
    )

    client.configure(
        InferenceConfiguration(
            confidence_threshold=CONFIDENCE,
            iou_threshold=IOU_THRESHOLD,
            api_key_transport="header",
        )
    )

    effective_inference_fps = max(
        1.0,
        CAMERA_FPS / PROCESS_EVERY_N_FRAMES,
    )

    tracker = sv.ByteTrack(
        track_activation_threshold=TRACK_ACTIVATION_THRESHOLD,
        lost_track_buffer=TRACK_LOST_BUFFER,
        minimum_matching_threshold=(
            TRACK_MINIMUM_MATCHING_THRESHOLD
        ),
        frame_rate=effective_inference_fps,
        minimum_consecutive_frames=(
            TRACK_MINIMUM_CONSECUTIVE_FRAMES
        ),
    )

    color_history = defaultdict(
        lambda: deque(
            maxlen=COLOR_HISTORY_LENGTH
        )
    )

    capture = open_video_source(VIDEO_SOURCE)

    success, frame = capture.read()

    if not success:
        capture.release()
        raise RuntimeError(
            "Camera opened, but no frame could be read."
        )

    output_writer = None
    if OUTPUT_VIDEO:
        source_fps = capture.get(cv2.CAP_PROP_FPS)
        if not source_fps or source_fps <= 0:
            source_fps = CAMERA_FPS
        frame_height, frame_width = frame.shape[:2]
        output_writer = cv2.VideoWriter(
            OUTPUT_VIDEO,
            cv2.VideoWriter_fourcc(*"mp4v"),
            source_fps,
            (frame_width, frame_height),
        )

    use_aoi = USE_AOI_DEFAULT
    aoi_polygon = (
        load_or_select_aoi_polygon(frame)
        if use_aoi
        else full_frame_polygon(frame)
    )
    warp_enabled = DEFAULT_WARP_ENABLED

    frame_index = 0
    latest_tracked_instances = []
    latest_inference_ms = 0.0
    last_error = ""
    latest_model_input = None
    last_debug_print = 0.0
    accuracy_recorder = (
        BBoxAccuracyRecorder(root=str(const.project_path(const.RECORDING_ROOT)))
        if BBoxAccuracyRecorder is not None
        else None
    )
    mqtt_publisher = None
    if const.MQTT_ENABLED:
        from .mqtt import PlateMqttPublisher

        mqtt_publisher = PlateMqttPublisher()
    capture_session_dir = None
    last_detection_capture = float("-inf")
    if CAPTURE_DETECTIONS:
        session_name = time.strftime("%Y%m%d_%H%M%S")
        capture_session_dir = os.path.join(CAPTURE_ROOT, session_name)
        os.makedirs(capture_session_dir, exist_ok=True)
        print(
            "Detection capture enabled:",
            os.path.abspath(capture_session_dir),
            f"interval={CAPTURE_INTERVAL_SECONDS:.2f}s",
        )

    while True:
        success, frame = capture.read()

        if not success:
            print("Camera frame could not be read.")
            break

        frame_index += 1

        if use_aoi:
            (
                original_aoi,
                masked_aoi,
                aoi_x,
                aoi_y,
                inverse_aoi_matrix,
                warp_active,
            ) = prepare_aoi_view(
                frame,
                aoi_polygon,
                warp_enabled,
            )
        else:
            aoi_polygon = full_frame_polygon(frame)
            original_aoi = frame
            masked_aoi = frame
            aoi_x = 0
            aoi_y = 0
            inverse_aoi_matrix = None
            warp_active = False

        if original_aoi is None or original_aoi.size == 0:
            print("AOI is empty.")
            break

        preprocess_mode = PREPROCESS_MODE

        model_input = preprocess_aoi_for_inference(
            masked_aoi,
        )

        should_run_inference = (
            frame_index == 1
            or frame_index % PROCESS_EVERY_N_FRAMES == 0
        )

        if should_run_inference:
            started = time.perf_counter()

            try:
                result = run_inference(
                    client,
                    model_input,
                )

                predictions, classes, best_confidence = (
                    describe_predictions(result)
                )

                latest_tracked_instances = (
                    process_predictions(
                        result=result,
                        # Use the same reference-calibrated brightness and
                        # contrast for classification/reflection filtering as
                        # for model inference. Geometry is unchanged.
                        original_aoi=model_input,
                        tracker=tracker,
                        color_history=color_history,
                    )
                )

                latest_inference_ms = (
                    time.perf_counter() - started
                ) * 1000.0

                if accuracy_recorder is not None:
                    accuracy_recorder.record(
                        frame_index=frame_index,
                        inference_ms=latest_inference_ms,
                        instances=latest_tracked_instances,
                        frame_shape=original_aoi.shape,
                    )
                    if mqtt_publisher is not None:
                        mqtt_events = mqtt_publisher.observe(
                            latest_tracked_instances,
                            original_aoi.shape,
                        )
                        for mqtt_event, plate_id, _plate_class in mqtt_events:
                            if mqtt_event in {"exit", "catch"}:
                                accuracy_recorder.finalize_disappearance(
                                    plate_id, mqtt_event
                                )

                last_error = ""
                latest_model_input = model_input.copy()

                now = time.time()
                if (
                    capture_session_dir is not None
                    and latest_tracked_instances
                    and now - last_detection_capture
                    >= CAPTURE_INTERVAL_SECONDS
                ):
                    try:
                        sample_id = save_detection_capture(
                            session_dir=capture_session_dir,
                            frame_index=frame_index,
                            before_preprocess=masked_aoi,
                            model_input=model_input,
                            predictions=predictions,
                            tracked_instances=latest_tracked_instances,
                            preprocess_mode=preprocess_mode,
                            warp_active=warp_active,
                            use_aoi=use_aoi,
                        )
                        last_detection_capture = now
                        print(
                            "Detection capture saved:",
                            sample_id,
                            f"tracked={len(latest_tracked_instances)}",
                            f"best={best_confidence:.2f}",
                        )
                    except Exception as capture_error:
                        print(
                            "Detection capture error:",
                            repr(capture_error),
                        )

                if now - last_debug_print >= 1.0:
                    print(
                        "Inference:",
                        f"predictions={len(predictions)}",
                        f"classes={dict(classes)}",
                        f"best={best_confidence:.2f}",
                        f"tracked={len(latest_tracked_instances)}",
                        f"target={TARGET_CLASS or 'ALL'}",
                        f"mode={preprocess_mode}",
                        f"warp={'on' if warp_active else 'off'}",
                        f"aoi={'on' if use_aoi else 'full'}",
                    )
                    last_debug_print = now

            except Exception as error:
                latest_tracked_instances = []
                last_error = str(error)
                latest_model_input = None
                print("Inference error:", repr(error))

        output = frame.copy()

        draw_aoi_overlay(
            output,
            aoi_polygon,
        )

        draw_tracked_instances(
            frame=output,
            tracked_instances=latest_tracked_instances,
            aoi_x=aoi_x,
            aoi_y=aoi_y,
            aoi_polygon=aoi_polygon,
            inverse_matrix=inverse_aoi_matrix,
        )

        draw_tracking_guides(
            output,
            aoi_polygon,
        )

        draw_summary_panel(
            frame=output,
            tracked_instances=latest_tracked_instances,
            inference_ms=latest_inference_ms,
            preprocess_mode=preprocess_mode,
            warp_active=warp_active,
            use_aoi=use_aoi,
            recorder_lines=(
                accuracy_recorder.display_lines()
                if accuracy_recorder is not None
                else None
            ),
        )

        if last_error:
            cv2.putText(
                output,
                "Inference error - see terminal",
                (15, 28),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (0, 0, 255),
                2,
                cv2.LINE_AA,
            )

        cv2.imshow(
            "Plate Segmentation + BBox + ByteTrack + HLS",
            output,
        )

        if output_writer is not None:
            output_writer.write(output)

        key = cv2.waitKey(1) & 0xFF

        if key == ord("q"):
            break

        if key == ord("b") and accuracy_recorder is not None:
            accuracy_recorder.start_batch()

        if accuracy_recorder is not None:
            if key == ord("1"):
                accuracy_recorder.mark_add("red")
            elif key == ord("2"):
                accuracy_recorder.mark_add("green")
            elif key == ord("3"):
                accuracy_recorder.mark_add("yellow")
            elif key == ord("0"):
                accuracy_recorder.mark_remove()

        if key == ord("a"):
            use_aoi = not use_aoi

            if use_aoi:
                aoi_polygon = load_or_select_aoi_polygon(frame)
            else:
                aoi_polygon = full_frame_polygon(frame)

            tracker.reset()
            color_history.clear()
            latest_tracked_instances = []
            latest_model_input = None

        if key == ord("r"):
            use_aoi = True
            aoi_polygon = select_aoi_polygon(frame)

            tracker.reset()
            color_history.clear()
            latest_tracked_instances = []
            latest_model_input = None

        if key == ord("w"):
            warp_enabled = not warp_enabled

            tracker.reset()
            color_history.clear()
            latest_tracked_instances = []
            latest_model_input = None

        if key == ord("s"):
            timestamp = int(time.time())

            raw_filename = (
                f"raw_aoi_{timestamp}.jpg"
            )

            output_filename = (
                f"segmentation_{timestamp}.jpg"
            )

            input_filename = (
                f"model_input_{preprocess_mode}_"
                f"warp_{int(warp_active)}_{timestamp}.jpg"
            )

            cv2.imwrite(
                raw_filename,
                original_aoi,
            )

            cv2.imwrite(
                output_filename,
                output,
            )

            if latest_model_input is not None:
                cv2.imwrite(
                    input_filename,
                    latest_model_input,
                )

            print("Saved:", raw_filename)
            print("Saved:", output_filename)
            if latest_model_input is not None:
                print("Saved:", input_filename)

    capture.release()
    if output_writer is not None:
        output_writer.release()
        print("Output saved:", OUTPUT_VIDEO)
    if accuracy_recorder is not None:
        accuracy_recorder.finalize()
    if mqtt_publisher is not None:
        mqtt_publisher.close()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
 
