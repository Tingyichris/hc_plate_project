"""Controller for the standalone 10 cm distance stability self-check.

This utility deliberately does not change the production detector, recorder,
MQTT publisher, or config/plate.toml.  Edit the SELF-CHECK SETTINGS below and
run:

    python main.py --camera 1

The existing camera/model/AOI settings are reused.  The selected AOI near edge
is treated as 0 cm and its far edge as TRACK_LENGTH_CM.  This is sufficient for
an operational self-check when those two AOI edges physically match the stated
locations.  It is not a ground-truth precision/recall test: without an
independent label or sensor, missed plates cannot be distinguished from empty
track.  The report therefore contains confidence and tracking stability proxy
metrics, not claimed model accuracy.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from collections import Counter, defaultdict, deque
from pathlib import Path

import cv2
import numpy as np
from cv2_enumerate_cameras import enumerate_cameras

# The detector and self-check now live in one project and share this package.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

from plate_detector import constants as const
from plate_detector import detector as det
from plate_detector import supervision as sv
from plate_detector.inference_sdk import InferenceConfiguration, InferenceHTTPClient
from plate_detector.camera_registry import binding_for, resolve_camera_index
from app.models.handoff import HandoffMQTT


# ---------------------------------------------------------------------------
# SELF-CHECK SETTINGS -- keep these settings here; no TOML entry is required.
# ---------------------------------------------------------------------------

REQUIRED_DETECTION_LENGTH_CM = 60.0
ENTRY_BUFFER_CM = 5.0
EXIT_BUFFER_CM = 10.0
TRACK_LENGTH_CM = (
    ENTRY_BUFFER_CM + REQUIRED_DETECTION_LENGTH_CM + EXIT_BUFFER_CM
)
DETECTION_START_CM = ENTRY_BUFFER_CM
DETECTION_END_CM = ENTRY_BUFFER_CM + REQUIRED_DETECTION_LENGTH_CM
BIN_SIZE_CM = 10.0
NEAR_DISTANCE_CM = 0.0

# Change this default or launch with --camera N. Use --list-cameras to discover
# Windows camera indices.
DEFAULT_CAMERA_INDEX = 1

CONFIDENCE_THRESHOLD = 0.35
IOU_THRESHOLD = 0.50

# A bin is provisionally stable when it has enough observations and its lower
# confidence tail remains above this limit.  These are proxy thresholds, not
# precision/recall thresholds.
MIN_OBSERVATIONS_PER_BIN = 20
MIN_CONFIDENCE_P10 = 0.50
MAX_TRACK_GAP_RATE = 0.15
MIN_COLOR_ACCURACY = 0.95
PLANNED_PASSES = 20
TRACK_SPEED_CM_PER_SECOND = 10.0
PASS_RECONNECT_MAX_SECONDS = 3.5
REMOVAL_SUSPECT_SECONDS = 0.35
REMOVAL_CONFIRM_MIN_SECONDS = 0.65
REMOVAL_CONFIRM_MAX_SECONDS = 1.50
PASS_RECONNECT_DISTANCE_TOLERANCE_CM = 15.0
# The belt is one-way.  Keep a small allowance for projection jitter, but do
# not let a following plate jump backwards into an older pass.
PASS_RECONNECT_BACKWARD_TOLERANCE_CM = 3.0
# Keep the production recorder's bbox-based identity check as a second signal.
# This prevents a new tracker from being attached to a nearby plate merely
# because both plates have a plausible conveyor distance.
PASS_RECONNECT_BOX_SCORE_MAX = 1.5
PASS_RECONNECT_BOX_GATE_SECONDS = 2.0
PASS_RECONNECT_COLOR_MIN_VOTES = 2
PASS_RECONNECT_COLOR_MIN_RATIO = 0.75
PASS_RECONNECT_COLOR_MIN_CONFIDENCE = 0.85
BASELINE_MIN_OBSERVATIONS = 2
# Two consecutive equal observations retain a consensus gate while avoiding the
# extra inference cycle that made the live map appear roughly two seconds late.
CONTINUOUS_MIN_PUBLISH_OBSERVATIONS = 2
CONTINUOUS_MAX_FIRST_DISTANCE_CM = DETECTION_START_CM + BIN_SIZE_CM

# Automatic anomaly evidence.
ANOMALY_LOW_CONFIDENCE = 0.65
ANOMALY_CAPTURE_COOLDOWN_SECONDS = 0.75
ANOMALY_MAX_EXPECTED_INSTANCES = 3

# Physical plate order used by this installation.
EXPECTED_COLOR_SEQUENCE = ("orange", "light_green", "yellow")
DISPLAY_COLOR_NAMES = {
    "orange": "O",
    "light_green": "G",
    "yellow": "Y",
    "unknown": "?",
}


def normalize_operational_colors(instance: dict) -> None:
    """Map installation-specific raw gray evidence to the green plate class."""
    raw_current = str(instance.get("current_color", "unknown") or "unknown")
    raw_stable = str(instance.get("stable_color", "unknown") or "unknown")
    instance["raw_current_color"] = raw_current
    instance["raw_stable_color"] = raw_stable
    if raw_current == "gray":
        instance["current_color"] = "light_green"
    if raw_stable == "gray":
        instance["stable_color"] = "light_green"

WINDOW_TITLE = (
    f"Plate distance self-check "
    f"(L{REQUIRED_DETECTION_LENGTH_CM:g}, R{TRACK_LENGTH_CM:g}, "
    f"E{ENTRY_BUFFER_CM:g}, X{EXIT_BUFFER_CM:g})"
)
OUTPUT_ROOT = PROJECT_ROOT / "outputs"
SELF_CHECK_AOI_FILE = PROJECT_ROOT / "self_check_aoi.json"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--camera",
        type=int,
        default=None,
        help=(
            "OpenCV camera index; when omitted, --camera-id is resolved from "
            "detector_config/camera_bindings.json"
        ),
    )
    parser.add_argument(
        "--list-cameras",
        action="store_true",
        help="probe camera indices and exit",
    )
    parser.add_argument(
        "--select-roi",
        action="store_true",
        help="draw and save this self-check project's four-point ROI",
    )
    parser.add_argument(
        "--handoff-role", choices=("off", "upstream", "downstream"),
        default=None, help="enable two-camera MQTT handoff validation",
    )
    parser.add_argument("--camera-id", default="", help="logical camera ID, e.g. cam-3")
    parser.add_argument(
        "--mqtt-camera-id",
        default="",
        help="MQTT camera ID; defaults to the saved binding or --camera-id",
    )
    parser.add_argument("--peer-camera-id", default="", help="other handoff camera ID")
    parser.add_argument("--mqtt-host", default=str(const.REPORT_MQTT_BROKER_HOST))
    parser.add_argument("--mqtt-port", type=int, default=int(const.REPORT_MQTT_BROKER_PORT))
    parser.add_argument(
        "--matching-mode", choices=("fifo",), default="fifo",
        help="cross-camera matching engine (production uses original FIFO)",
    )
    parser.add_argument(
        "--continuous-handoff", action="store_true",
        help="always-on unknown-count/color handoff mode; does not require B",
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="production mode: no window or keyboard controls; continuous recording",
    )
    parser.add_argument(
        "--autosave-seconds", type=float, default=5.0,
        help="live snapshot interval in seconds (default: 5)",
    )
    return parser.parse_args()


def save_self_check_aoi(polygon: np.ndarray, frame_shape) -> None:
    height, width = frame_shape[:2]
    normalized = [
        [float(point[0]) / width, float(point[1]) / height]
        for point in np.asarray(polygon)
    ]
    with SELF_CHECK_AOI_FILE.open("w", encoding="utf-8") as handle:
        json.dump(normalized, handle, indent=2)


def load_self_check_aoi(frame_shape):
    if not SELF_CHECK_AOI_FILE.exists():
        return None
    with SELF_CHECK_AOI_FILE.open("r", encoding="utf-8") as handle:
        normalized = json.load(handle)
    if not isinstance(normalized, list) or len(normalized) != 4:
        return None
    height, width = frame_shape[:2]
    polygon = np.asarray(
        [[point[0] * width, point[1] * height] for point in normalized],
        dtype=np.int32,
    )
    polygon[:, 0] = np.clip(polygon[:, 0], 0, width - 1)
    polygon[:, 1] = np.clip(polygon[:, 1], 0, height - 1)
    return polygon


def select_self_check_aoi(frame: np.ndarray) -> np.ndarray:
    points = []
    window_name = "Draw Self-Check ROI (exactly 4 points)"

    def mouse_callback(event, x, y, _flags, _param):
        if event == cv2.EVENT_LBUTTONDOWN and len(points) < 4:
            points.append((x, y))
        elif event == cv2.EVENT_RBUTTONDOWN and points:
            points.pop()

    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(window_name, mouse_callback)
    while True:
        preview = frame.copy()
        for index, point in enumerate(points, start=1):
            cv2.circle(preview, point, 6, (0, 255, 255), -1)
            cv2.putText(
                preview, str(index), (point[0] + 8, point[1] - 8),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 2,
                cv2.LINE_AA,
            )
        if len(points) >= 2:
            cv2.polylines(
                preview, [np.asarray(points, dtype=np.int32)], len(points) == 4,
                (0, 255, 255), 2, cv2.LINE_AA,
            )
        cv2.putText(
            preview,
            "Left:add  Right:undo  Enter:save  Esc:cancel",
            (15, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
            (255, 255, 255), 2, cv2.LINE_AA,
        )
        cv2.imshow(window_name, preview)
        key = cv2.waitKey(20) & 0xFF
        if key in (10, 13) and len(points) == 4:
            polygon = np.asarray(points, dtype=np.int32)
            save_self_check_aoi(polygon, frame.shape)
            cv2.destroyWindow(window_name)
            print("Self-check ROI saved:", SELF_CHECK_AOI_FILE)
            return polygon
        if key == 27:
            cv2.destroyWindow(window_name)
            raise RuntimeError("Self-check ROI selection was cancelled.")


def camera_info_by_index():
    return {
        camera.index: camera
        for camera in enumerate_cameras(cv2.CAP_DSHOW)
    }


def _read_warmed_frame(capture, attempts=30):
    last_frame = None
    for _ in range(attempts):
        ok, frame = capture.read()
        if ok and frame is not None:
            last_frame = frame
            # Reject UVC startup frames that are technically readable but
            # completely black.  OBS performs a similar warm-up implicitly.
            if float(frame.mean()) > 2.0 and int(frame.max()) > 10:
                return frame
        time.sleep(0.04)
    return last_frame


def _create_capture(camera_index: int, force_mjpeg: bool):
    backend = cv2.CAP_DSHOW if sys.platform.startswith("win") else cv2.CAP_ANY
    capture = cv2.VideoCapture(camera_index, backend)
    if capture.isOpened() and force_mjpeg:
        # Both OpenCam units expose 1280x720 reliably to OBS as MJPEG.  Asking
        # OpenCV only for width/height/FPS may negotiate a black YUY2 stream.
        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, det.CAMERA_WIDTH)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, det.CAMERA_HEIGHT)
        capture.set(cv2.CAP_PROP_FPS, det.CAMERA_FPS)
    return capture


def open_camera(camera_index: int):
    info = camera_info_by_index().get(camera_index)
    device_label = info.name if info is not None else "unknown device"
    for force_mjpeg in (True, False):
        capture = _create_capture(camera_index, force_mjpeg)
        if not capture.isOpened():
            capture.release()
            continue
        frame = _read_warmed_frame(capture)
        if frame is not None and float(frame.mean()) > 2.0 and int(frame.max()) > 10:
            mode = "MJPEG 1280x720" if force_mjpeg else "device default"
            print(
                f"Camera {camera_index} ({device_label}) opened with {mode}; "
                f"frame={frame.shape[1]}x{frame.shape[0]} mean={frame.mean():.1f}"
            )
            return capture, frame
        capture.release()

    info_hint = f" ({info.path})" if info is not None and info.path else ""
    raise RuntimeError(
        f"Camera index {camera_index} ({device_label}) opened no usable non-black "
        f"stream{info_hint}. Run 'python main.py --list-cameras'. "
        f"Close Camera/Teams/OBS/other detector processes if they own the device."
    )


def list_cameras() -> None:
    found = []
    cameras = list(enumerate_cameras(cv2.CAP_DSHOW))
    print(f"DirectShow enumerated {len(cameras)} camera devices:")
    for info in cameras:
        print(f"  camera {info.index}: {info.name}")
        if info.path:
            print(f"    path: {info.path}")
        try:
            capture, frame = open_camera(info.index)
            height, width = frame.shape[:2]
            found.append(info.index)
            print(f"    stream: usable ({width}x{height}, mean={frame.mean():.1f})")
            capture.release()
        except RuntimeError as error:
            print(f"    stream: unusable ({error})")
    if not found:
        print("No readable camera found. Close apps using the cameras and retry.")
    else:
        print("Launch example:")
        print(f"  python main.py --camera {found[0]}")


def build_world_transform(aoi_polygon: np.ndarray):
    """Return image<->world homographies for the four-point conveyor AOI."""
    ordered = det.order_quad_points(aoi_polygon)
    # order_quad_points returns TL, TR, BR, BL.  The narrower/top edge is the
    # far end in the current installation; Y=TRACK_LENGTH_CM at that edge.
    world = np.asarray(
        [
            [0.0, TRACK_LENGTH_CM],
            [1.0, TRACK_LENGTH_CM],
            [1.0, NEAR_DISTANCE_CM],
            [0.0, NEAR_DISTANCE_CM],
        ],
        dtype=np.float32,
    )
    image_to_world = cv2.getPerspectiveTransform(ordered, world)
    world_to_image = cv2.getPerspectiveTransform(world, ordered)
    return image_to_world, world_to_image


def transform_points(points: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    points = np.asarray(points, dtype=np.float32).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(points, matrix).reshape(-1, 2)


def distance_bin_start(distance_cm: float) -> float:
    """Return a 10 cm bin aligned to the formal decision interval."""
    bounded = float(np.clip(distance_cm, DETECTION_START_CM, DETECTION_END_CM))
    if math.isclose(bounded, DETECTION_END_CM):
        bounded = max(DETECTION_START_CM, bounded - 1e-6)
    offset = bounded - DETECTION_START_CM
    return DETECTION_START_CM + math.floor(offset / BIN_SIZE_CM) * BIN_SIZE_CM


def distance_bin_end(bin_start_cm: float) -> float:
    """Cap the final distance bin at the decision line."""
    return min(DETECTION_END_CM, bin_start_cm + BIN_SIZE_CM)


def draw_distance_grid(frame: np.ndarray, world_to_image: np.ndarray) -> None:
    distances = [NEAR_DISTANCE_CM, TRACK_LENGTH_CM]
    distances.extend(
        np.arange(
            DETECTION_START_CM,
            DETECTION_END_CM + 1e-6,
            BIN_SIZE_CM,
            dtype=float,
        )
    )
    distances.extend([DETECTION_START_CM, DETECTION_END_CM])
    distances = sorted(set(distances))
    for distance in distances:
        world_line = np.asarray([[0.0, distance], [1.0, distance]], np.float32)
        image_line = np.rint(transform_points(world_line, world_to_image)).astype(int)
        p1 = tuple(image_line[0])
        p2 = tuple(image_line[1])
        is_roi_end = math.isclose(distance, NEAR_DISTANCE_CM) or math.isclose(
            distance, TRACK_LENGTH_CM
        )
        is_detection_edge = math.isclose(
            distance, DETECTION_START_CM
        ) or math.isclose(distance, DETECTION_END_CM)
        if is_roi_end:
            color = (0, 210, 255)
        elif is_detection_edge:
            color = (0, 255, 0)
        else:
            color = (160, 160, 70)
        cv2.line(
            frame,
            p1,
            p2,
            color,
            2 if is_roi_end or is_detection_edge else 1,
            cv2.LINE_AA,
        )
        label_x = int((p1[0] + p2[0]) / 2)
        label_y = int((p1[1] + p2[1]) / 2)
        cv2.putText(
            frame,
            (
                f"{distance:.0f} cm "
                + (
                    "ENTRY"
                    if math.isclose(distance, DETECTION_START_CM)
                    else "DECISION END"
                    if math.isclose(distance, DETECTION_END_CM)
                    else ""
                )
            ).rstrip(),
            (label_x + 5, label_y - 3),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            color,
            1,
            cv2.LINE_AA,
        )


class SelfCheckRecorder:
    def __init__(self, handoff=None, external_expected: bool = False,
                 session_suffix: str = "", continuous_mode: bool = False):
        session_name = (
            time.strftime("%Y%m%d_%H%M%S")
            + f"_L{REQUIRED_DETECTION_LENGTH_CM:g}"
            + f"_R{TRACK_LENGTH_CM:g}"
            + f"_E{ENTRY_BUFFER_CM:g}"
            + f"_X{EXIT_BUFFER_CM:g}"
            + (f"_{session_suffix}" if session_suffix else "")
        )
        self.directory = OUTPUT_ROOT / session_name
        self.directory.mkdir(parents=True, exist_ok=True)
        self.started = time.perf_counter()
        self.rows: list[dict] = []
        self.by_bin: dict[float, list[float]] = defaultdict(list)
        self.bin_tracker_ids: dict[float, set[int]] = defaultdict(set)
        self.last_seen_inference: dict[int, int] = {}
        self.tracker_gaps: dict[float, int] = defaultdict(int)
        self.tracker_observations: dict[float, int] = defaultdict(int)
        self.evaluation_active = bool(continuous_mode)
        self.color_samples: list[dict] = []
        self.tracker_to_pass: dict[int, int] = {}
        self.pass_states: dict[int, dict] = {}
        self.next_pass_id = 1
        self.next_expected_index = 0
        self.current_lap_id = 1 if continuous_mode else 0
        self.handoff = handoff
        self.external_expected = external_expected
        self.continuous_mode = continuous_mode

    def start_color_cycle(self) -> None:
        self.current_lap_id += 1
        self.evaluation_active = True
        self.next_expected_index = 0
        # A new lap must not reconnect to a plate from the previous lap, but
        # all prior observations remain available for the final report.
        self.tracker_to_pass.clear()
        self.last_seen_inference.clear()
        print(
            f"Lap {self.current_lap_id} armed: next plates must be "
            "ORANGE, GREEN, YELLOW. Previous laps are preserved."
        )
        if self.handoff is not None:
            self.handoff.start_lap(self.current_lap_id)

    @staticmethod
    def _box_match_score(first, second):
        if first is None or second is None:
            return None
        ax = (float(first[0]) + float(first[2])) / 2.0
        ay = (float(first[1]) + float(first[3])) / 2.0
        bx = (float(second[0]) + float(second[2])) / 2.0
        by = (float(second[1]) + float(second[3])) / 2.0
        scale = max(
            20.0,
            math.hypot(
                float(second[2]) - float(second[0]),
                float(second[3]) - float(second[1]),
            ),
        )
        return math.hypot(ax - bx, ay - by) / scale

    def _new_pass(self, tracker_id, distance, now, inference_index, box=None):
        pass_id = self.next_pass_id
        self.next_pass_id += 1
        expected_color = ""
        if not self.external_expected and not self.continuous_mode:
            expected_color = EXPECTED_COLOR_SEQUENCE[self.next_expected_index]
            self.next_expected_index = (
                self.next_expected_index + 1
            ) % len(EXPECTED_COLOR_SEQUENCE)
        state = {
            "pass_id": pass_id,
            "lap_id": self.current_lap_id,
            "expected_color": expected_color,
            "last_distance": distance,
            "min_distance": distance,
            "last_seen": now,
            "first_seen_unix": time.time(),
            "last_inference": inference_index,
            "last_box": box,
            "tracker_ids": {tracker_id},
            "measurement_started": False,
            "decision_locked": False,
            "final_color": None,
            "final_observations": 0,
            "final_confidence_median": None,
            "stage": "approaching",
            "missing_since": None,
            "removal_suspected": False,
            # This is strictly this camera's own colour history.  In
            # downstream mode the MQTT reference colour is deliberately not
            # used for identity matching, so the validation remains blind.
            "local_color_votes": Counter(),
        }
        self.pass_states[pass_id] = state
        self.tracker_to_pass[tracker_id] = pass_id
        print(
            f"New pass P{pass_id}: expected="
            f"{DISPLAY_COLOR_NAMES.get(expected_color, expected_color) or 'upstream'}"
        )
        return state

    @staticmethod
    def _stable_local_color(state):
        votes = state.get("local_color_votes") or Counter()
        known = Counter({
            color: count for color, count in votes.items()
            if color and color != "unknown"
        })
        total = sum(known.values())
        if total < PASS_RECONNECT_COLOR_MIN_VOTES:
            return None
        color, count = known.most_common(1)[0]
        if count / total < PASS_RECONNECT_COLOR_MIN_RATIO:
            return None
        return color

    def _assign_pass(
        self, tracker_id, distance, now, inference_index, assigned_this_inference,
        box=None, local_color=None,
    ):
        mapped_pass_id = self.tracker_to_pass.get(tracker_id)
        if mapped_pass_id is not None and mapped_pass_id in assigned_this_inference:
            # A duplicate detection carrying the same tracker ID must not be
            # allowed to create or steal another persistent pass ID.
            return None
        if mapped_pass_id is not None:
            state = self.pass_states[mapped_pass_id]
            if state["stage"] in {"exited", "caught"}:
                return None
        else:
            candidates = []
            for pass_id, candidate in self.pass_states.items():
                if pass_id in assigned_this_inference:
                    continue
                if int(candidate["lap_id"]) != self.current_lap_id:
                    continue
                if candidate["stage"] in {"exited", "caught"}:
                    continue
                elapsed = now - float(candidate["last_seen"])
                if elapsed < 0 or elapsed > PASS_RECONNECT_MAX_SECONDS:
                    continue
                last_distance = float(candidate["last_distance"])
                if distance < last_distance - PASS_RECONNECT_BACKWARD_TOLERANCE_CM:
                    continue
                predicted_distance = min(
                    TRACK_LENGTH_CM,
                    last_distance + TRACK_SPEED_CM_PER_SECOND * elapsed,
                )
                error = abs(distance - predicted_distance)
                if error > PASS_RECONNECT_DISTANCE_TOLERANCE_CM:
                    continue
                box_score = self._box_match_score(box, candidate.get("last_box"))
                candidate_color = self._stable_local_color(candidate)
                color_matches = (
                    candidate_color is not None
                    and local_color not in (None, "", "unknown")
                    and local_color == candidate_color
                )
                color_mismatches = (
                    candidate_color is not None
                    and local_color not in (None, "", "unknown")
                    and local_color != candidate_color
                )
                if color_mismatches:
                    # A pass with a strong local colour history must not be
                    # stolen by a close following plate of another colour.
                    continue
                if (
                    box_score is not None
                    and elapsed <= PASS_RECONNECT_BOX_GATE_SECONDS
                    and box_score > PASS_RECONNECT_BOX_SCORE_MAX
                    and not color_matches
                ):
                    continue
                # Distance remains the primary conveyor-motion signal.  The
                # production-style bbox score resolves nearby candidates.
                candidates.append(
                    (error, box_score if box_score is not None else math.inf,
                     -last_distance, pass_id, candidate)
                )
            if candidates:
                _error, _box_score, _neg_distance, mapped_pass_id, state = min(candidates)
                self.tracker_to_pass[tracker_id] = mapped_pass_id
                state["tracker_ids"].add(tracker_id)
            else:
                # A brand-new detection beyond the decision line is not a new
                # plate.  It is either an unmatched tail or an edge artefact.
                if distance >= DETECTION_END_CM:
                    return None
                state = self._new_pass(
                    tracker_id, distance, now, inference_index, box=box
                )

        if state.get("removal_suspected") and self.handoff is not None:
            self.handoff.revoke_local_removal(state)
        state["missing_since"] = None
        state["removal_suspected"] = False
        state["last_distance"] = max(float(state["last_distance"]), distance)
        state["min_distance"] = min(float(state["min_distance"]), distance)
        state["last_seen"] = now
        state["last_inference"] = inference_index
        if box is not None:
            state["last_box"] = box
        assigned_this_inference.add(int(state["pass_id"]))
        return state

    def _lock_pass_result(self, state: dict, inference_index: int) -> None:
        """Freeze the pass result on its first crossing of the decision line."""
        if state["decision_locked"]:
            return
        pass_id = int(state["pass_id"])
        rows = [row for row in self.rows if int(row["pass_id"]) == pass_id]
        state["decision_locked"] = True
        state["stage"] = "exit_candidate"
        state["final_observations"] = len(rows)
        if rows:
            votes = Counter(row["predicted_color"] for row in rows)
            state["final_color"] = votes.most_common(1)[0][0]
            state["final_confidence_median"] = float(
                np.median([float(row["confidence"]) for row in rows])
            )
        print(
            f"Pass P{pass_id} locked at {DETECTION_END_CM:g} cm: "
            f"color={state['final_color'] or 'none'} "
            f"observations={state['final_observations']}"
        )
        if self.handoff is not None:
            if self.external_expected:
                self.handoff.finalize_downstream(state)
            else:
                publish_allowed = True
                if self.continuous_mode:
                    publish_allowed = (
                        int(state.get("final_observations", 0))
                        >= CONTINUOUS_MIN_PUBLISH_OBSERVATIONS
                        and float(state.get("min_distance", TRACK_LENGTH_CM))
                        <= CONTINUOUS_MAX_FIRST_DISTANCE_CM
                    )
                if publish_allowed:
                    if self.continuous_mode:
                        self.handoff.publish_final_plate(state)
                    else:
                        self.handoff.publish_locked_plate(state)
                else:
                    state["publish_rejected"] = True
                    print(
                        f"Pass P{pass_id} not published: entrance evidence "
                        f"is insufficient (observations={state['final_observations']}, "
                        f"first={state.get('min_distance')} cm)"
                    )

    def _publish_early_handoff(self, state: dict) -> None:
        """Announce a continuous-mode upstream plate after two stable votes."""
        if (
            self.handoff is None
            or self.external_expected
            or not self.continuous_mode
            or state.get("decision_locked")
            or float(state.get("min_distance", TRACK_LENGTH_CM))
            > CONTINUOUS_MAX_FIRST_DISTANCE_CM
        ):
            return
        pass_id = int(state["pass_id"])
        rows = [row for row in self.rows if int(row["pass_id"]) == pass_id]
        if len(rows) < CONTINUOUS_MIN_PUBLISH_OBSERVATIONS:
            return
        recent = rows[-CONTINUOUS_MIN_PUBLISH_OBSERVATIONS:]
        colors = [str(row.get("predicted_color", "")) for row in recent]
        if not colors[0] or any(color != colors[0] for color in colors):
            return
        self.handoff.publish_detected_plate(
            state,
            colors[0],
            float(np.median([float(row["confidence"]) for row in rows])),
            len(rows),
        )

    def _finalize_missing_passes(
        self,
        visible_pass_ids: set[int],
        now: float,
        inference_index: int,
    ) -> None:
        """Finalize an exit/catch only after the normal reconnect window."""
        for pass_id, state in self.pass_states.items():
            if state["stage"] in {"exited", "caught"} or pass_id in visible_pass_ids:
                continue
            if state["missing_since"] is None:
                state["missing_since"] = now
            missing_seconds = now - float(state["last_seen"])
            if (
                self.handoff is not None
                and not state["decision_locked"]
                and missing_seconds >= REMOVAL_SUSPECT_SECONDS
                and not state.get("removal_suspected")
            ):
                state["removal_suspected"] = True
                self.handoff.suspect_local_removal(state)
            remaining_cm = max(
                0.0, DETECTION_END_CM - float(state.get("last_distance", 0.0))
            )
            dynamic_confirm_seconds = min(
                REMOVAL_CONFIRM_MAX_SECONDS,
                max(
                    REMOVAL_CONFIRM_MIN_SECONDS,
                    (remaining_cm / TRACK_SPEED_CM_PER_SECOND) * 0.35,
                ),
            )
            confirm_seconds = (
                dynamic_confirm_seconds
                if not state["decision_locked"]
                else PASS_RECONNECT_MAX_SECONDS
            )
            if missing_seconds <= confirm_seconds:
                continue
            state["stage"] = "exited" if state["decision_locked"] else "caught"
            if (
                self.handoff is not None
                and self.external_expected
                and not state["decision_locked"]
            ):
                self.handoff.finalize_downstream_removed(state)
            if (
                self.handoff is not None
                and not self.external_expected
                and self.continuous_mode
                and not state["decision_locked"]
            ):
                self.handoff.publish_removed_plate(state)

    def _record_color_calibration(
        self,
        inference_index: int,
        instance: dict,
        color_image: np.ndarray,
    ) -> None:
        polygon = np.rint(instance["polygon"]).astype(np.int32)
        mask = np.zeros(color_image.shape[:2], dtype=np.uint8)
        cv2.fillPoly(mask, [polygon], 255)
        x, y, width, height = cv2.boundingRect(polygon)
        erosion = max(1, int(round(min(width, height) * 0.06)))
        kernel_size = max(3, erosion * 2 + 1)
        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (kernel_size, kernel_size)
        )
        inner_mask = cv2.erode(mask, kernel, iterations=1)
        if np.count_nonzero(inner_mask) < det.MIN_COLOR_PIXELS:
            inner_mask = mask

        hls = cv2.cvtColor(color_image, cv2.COLOR_BGR2HLS)
        lab = cv2.cvtColor(color_image, cv2.COLOR_BGR2LAB)
        hue, lightness, saturation = cv2.split(hls)
        sample = (
            (inner_mask > 0)
            & (saturation >= det.MIN_COLOR_SATURATION)
            & (lightness >= det.MIN_COLOR_LIGHTNESS)
            & (lightness <= det.MAX_COLOR_LIGHTNESS)
        )
        sample_count = int(np.count_nonzero(sample))
        if sample_count < det.MIN_COLOR_PIXELS:
            return

        hue_values = hue[sample].astype(np.float32)
        lightness_values = lightness[sample].astype(np.float32)
        saturation_values = saturation[sample].astype(np.float32)
        a_values = lab[:, :, 1][sample].astype(np.float32)
        b_values = lab[:, :, 2][sample].astype(np.float32)
        self.color_samples.append(
            {
                "inference_index": inference_index,
                "tracker_id": int(instance["tracker_id"]),
                "pass_id": int(instance["pass_id"]),
                "lap_id": int(instance["lap_id"]),
                "distance_cm": round(float(instance["distance_cm"]), 3),
                "distance_bin_start_cm": round(
                    distance_bin_start(float(instance["distance_cm"])), 1
                ),
                "expected_color": str(instance["expected_color"]),
                "predicted_color": str(instance.get("stable_color", "unknown")),
                "confidence": round(float(instance["confidence"]), 6),
                "sample_pixels": sample_count,
                "h_p10": round(float(np.percentile(hue_values, 10)), 3),
                "h_median": round(float(np.median(hue_values)), 3),
                "h_p90": round(float(np.percentile(hue_values, 90)), 3),
                "l_median": round(float(np.median(lightness_values)), 3),
                "s_median": round(float(np.median(saturation_values)), 3),
                "lab_a_median": round(float(np.median(a_values)), 3),
                "lab_b_median": round(float(np.median(b_values)), 3),
            }
        )

    def record(
        self,
        inference_index: int,
        instances: list[dict],
        color_image: np.ndarray,
    ) -> None:
        # Do not create misleading observations before the operator aligns the
        # known O-G-Y sequence and explicitly starts the test with B.
        if not self.evaluation_active:
            return
        now = time.perf_counter() - self.started
        assigned_this_inference = set()
        ordered_instances = sorted(
            instances,
            key=lambda item: (
                int(item["tracker_id"]) not in self.tracker_to_pass,
                -float(item["distance_cm"]),
            ),
        )
        for instance in ordered_instances:
            distance = float(instance["distance_cm"])
            bin_start = distance_bin_start(distance)
            tracker_id = int(instance["tracker_id"])
            confidence = float(instance["confidence"])
            raw_box = instance.get("xyxy")
            box = (
                tuple(float(value) for value in raw_box)
                if raw_box is not None
                else None
            )
            pass_state = self._assign_pass(
                tracker_id,
                distance,
                now,
                inference_index,
                assigned_this_inference,
                box=box,
                local_color=str(instance.get("stable_color", "unknown")),
            )
            if pass_state is None:
                instance["measurement_phase"] = "exit_buffer_unmatched"
                continue
            pass_id = int(pass_state["pass_id"])
            lap_id = int(pass_state["lap_id"])
            expected_color = str(pass_state["expected_color"])
            # The live map must not wait for the 5 cm measurement buffer and
            # two-row reporting gate. The detector's stable colour plus a high
            # confidence observation is enough to create a reversible,
            # provisional lifecycle; the normal votes below still confirm it.
            if (
                self.handoff is not None
                and not self.external_expected
                and self.continuous_mode
            ):
                provisional_color = str(
                    instance.get("stable_color", "unknown") or "unknown"
                )
                if (
                    provisional_color != "unknown"
                    and confidence >= PASS_RECONNECT_COLOR_MIN_CONFIDENCE
                ):
                    self.handoff.publish_detected_plate(
                        pass_state, provisional_color, confidence, 1
                    )
            if (
                self.handoff is not None
                and self.external_expected
                and not expected_color
            ):
                upstream_color = self.handoff.match_downstream_entry(pass_state)
                if upstream_color:
                    pass_state["expected_color"] = upstream_color
                    expected_color = upstream_color
                    binding = self.handoff.downstream_binding(pass_id)
                    if binding is not None:
                        pass_state["upstream_pass_id"] = binding.get(
                            "upstream_pass_id"
                        )
                        pass_state["upstream_sequence"] = binding.get("sequence")
                        pass_state["upstream_published_at"] = binding.get("timestamp")
                        pass_state["mqtt_received_at"] = binding.get("received_at")
            instance["pass_id"] = pass_id
            instance["lap_id"] = lap_id
            instance["expected_color"] = expected_color
            if distance < DETECTION_START_CM:
                instance["measurement_phase"] = "entry_buffer"
                continue
            if distance >= DETECTION_END_CM:
                instance["measurement_phase"] = "exit_buffer"
                self._lock_pass_result(pass_state, inference_index)
                continue
            if pass_state["decision_locked"]:
                instance["measurement_phase"] = "exit_buffer_locked"
                continue
            if not pass_state["measurement_started"]:
                pass_state["measurement_started"] = True
                pass_state["stage"] = "entered"
            instance["measurement_phase"] = "decision"
            predicted_color = instance.get("stable_color", "unknown")
            if (
                predicted_color
                and predicted_color != "unknown"
                and confidence >= PASS_RECONNECT_COLOR_MIN_CONFIDENCE
            ):
                pass_state["local_color_votes"][str(predicted_color)] += 1
            if self.handoff is not None and self.external_expected:
                stable_local_color = self._stable_local_color(pass_state)
                if stable_local_color:
                    self.handoff.publish_downstream_entry(
                        pass_state, stable_local_color
                    )
            color_correct = (
                int(predicted_color == expected_color) if expected_color else ""
            )
            previous = self.last_seen_inference.get(tracker_id)
            if previous is not None and inference_index - previous > 1:
                self.tracker_gaps[bin_start] += 1
            self.last_seen_inference[tracker_id] = inference_index
            self.tracker_observations[bin_start] += 1
            self.by_bin[bin_start].append(confidence)
            self.bin_tracker_ids[bin_start].add(tracker_id)
            self.rows.append(
                {
                    "session_s": round(now, 4),
                    "wall_timestamp": time.time(),
                    "inference_index": inference_index,
                    "tracker_id": tracker_id,
                    "pass_id": pass_id,
                    "lap_id": lap_id,
                    "distance_cm": round(distance, 3),
                    "distance_bin_start_cm": round(bin_start, 1),
                    "distance_bin_end_cm": round(distance_bin_end(bin_start), 1),
                    "confidence": round(confidence, 6),
                    "predicted_color": predicted_color,
                    "current_color": instance.get("current_color", "unknown"),
                    "raw_predicted_color": instance.get(
                        "raw_stable_color", predicted_color
                    ),
                    "raw_current_color": instance.get(
                        "raw_current_color",
                        instance.get("current_color", "unknown"),
                    ),
                    "expected_color": expected_color,
                    "upstream_pass_id": pass_state.get("upstream_pass_id", ""),
                    "upstream_sequence": pass_state.get("upstream_sequence", ""),
                    "upstream_published_at": pass_state.get(
                        "upstream_published_at", ""
                    ),
                    "mqtt_received_at": pass_state.get("mqtt_received_at", ""),
                    "color_correct": color_correct,
                }
            )
            self._publish_early_handoff(pass_state)
            self._record_color_calibration(
                inference_index, instance, color_image
            )
        self._finalize_missing_passes(
            {int(value) for value in assigned_this_inference},
            now,
            inference_index,
        )

    def pass_bin_results(self) -> list[dict]:
        grouped = defaultdict(list)
        for row in self.rows:
            grouped[(int(row["pass_id"]), float(row["distance_bin_start_cm"]))].append(row)
        results = []
        for (pass_id, bin_start), rows in sorted(grouped.items()):
            votes = Counter(row["predicted_color"] for row in rows)
            majority_color, majority_count = votes.most_common(1)[0]
            expected_color = rows[0]["expected_color"]
            confidences = [float(row["confidence"]) for row in rows]
            results.append(
                {
                    "pass_id": pass_id,
                    "lap_id": int(rows[0]["lap_id"]),
                    "expected_color": expected_color,
                    "distance_bin_start_cm": bin_start,
                    "distance_bin_end_cm": distance_bin_end(bin_start),
                    "observation_count": len(rows),
                    "majority_color": majority_color,
                    "majority_count": majority_count,
                    "majority_ratio": round(majority_count / len(rows), 5),
                    "color_correct": (
                        int(majority_color == expected_color)
                        if expected_color else ""
                    ),
                    "confidence_median": round(float(np.median(confidences)), 5),
                    "tracker_ids": " ".join(
                        str(value)
                        for value in sorted({int(row["tracker_id"]) for row in rows})
                    ),
                }
            )
        return results

    def metrics(self) -> list[dict]:
        metrics = []
        pass_results = self.pass_bin_results()
        def color_ok(row):
            value = row.get("color_correct", "")
            return True if value in ("", None) else bool(int(value))
        identified_passes = len(self.pass_states)
        baseline_results = [
            row for row in pass_results
            if row["distance_bin_start_cm"] == round(DETECTION_START_CM, 1)
        ]
        # A one-frame entrance artefact is not a physical reference plate.
        # Requiring repeat observations stops that transient ID from making
        # every later 10 cm segment fail continuity.
        pass_observation_totals = Counter(
            int(row["pass_id"]) for row in self.rows
        )
        baseline_pass_ids = {
            int(row["pass_id"])
            for row in baseline_results
            if pass_observation_totals[int(row["pass_id"])]
            >= BASELINE_MIN_OBSERVATIONS
        }
        continuous_range = bool(baseline_pass_ids)
        distance = DETECTION_START_CM
        while distance < DETECTION_END_CM - 1e-6:
            confidences = np.asarray(self.by_bin.get(distance, []), dtype=float)
            observations = int(confidences.size)
            gaps = int(self.tracker_gaps.get(distance, 0))
            gap_rate = gaps / max(1, self.tracker_observations.get(distance, 0))
            evaluated_rows = [
                row for row in self.rows
                if row["distance_bin_start_cm"] == round(distance, 1)
                and row["expected_color"]
            ]
            color_total = len(evaluated_rows)
            color_correct = sum(int(row["color_correct"]) for row in evaluated_rows)
            color_accuracy = color_correct / color_total if color_total else None
            bin_pass_results = [
                row for row in pass_results
                if row["distance_bin_start_cm"] == round(distance, 1)
                and pass_observation_totals[int(row["pass_id"])]
                >= BASELINE_MIN_OBSERVATIONS
            ]
            detected_passes = len(bin_pass_results)
            evaluated_pass_results = [
                row for row in bin_pass_results if row.get("expected_color")
            ]
            correct_passes = sum(
                int(row["color_correct"]) for row in evaluated_pass_results
            )
            bin_by_pass = {
                int(row["pass_id"]): row for row in bin_pass_results
            }
            bin_pass_ids = set(bin_by_pass)
            missing_baseline_ids = baseline_pass_ids - bin_pass_ids
            extra_pass_ids = bin_pass_ids - baseline_pass_ids
            same_plate_ids = (
                bool(baseline_pass_ids)
                and not missing_baseline_ids
                and not extra_pass_ids
            )
            color_consistent_passes = sum(
                int(color_ok(bin_by_pass[pass_id]))
                for pass_id in baseline_pass_ids & bin_pass_ids
            )
            all_plate_colors_consistent = (
                bool(baseline_pass_ids)
                and color_consistent_passes == len(baseline_pass_ids)
            )
            range_qualified = same_plate_ids and all_plate_colors_consistent
            continuous_range = continuous_range and range_qualified
            pass_accuracy = (
                correct_passes / len(evaluated_pass_results)
                if evaluated_pass_results else None
            )
            per_color = {}
            for color in EXPECTED_COLOR_SEQUENCE:
                color_rows = [
                    row for row in evaluated_rows if row["expected_color"] == color
                ]
                correct = sum(int(row["color_correct"]) for row in color_rows)
                per_color[color] = (
                    correct / len(color_rows) if color_rows else None
                )
            p10 = float(np.percentile(confidences, 10)) if observations else None
            median = float(np.median(confidences)) if observations else None
            p90 = float(np.percentile(confidences, 90)) if observations else None
            if observations < MIN_OBSERVATIONS_PER_BIN:
                status = "insufficient"
            elif color_total and color_accuracy < MIN_COLOR_ACCURACY:
                status = "unstable"
            elif p10 is not None and p10 < MIN_CONFIDENCE_P10:
                status = "unstable"
            elif gap_rate > MAX_TRACK_GAP_RATE:
                status = "unstable"
            else:
                status = "stable"
            metrics.append(
                {
                    "distance_start_cm": round(distance, 1),
                    "distance_end_cm": round(distance_bin_end(distance), 1),
                    "observations": observations,
                    "unique_tracker_ids": len(self.bin_tracker_ids.get(distance, set())),
                    "confidence_p10": round(p10, 5) if p10 is not None else None,
                    "confidence_median": round(median, 5) if median is not None else None,
                    "confidence_p90": round(p90, 5) if p90 is not None else None,
                    "color_correct": color_correct,
                    "color_total": color_total,
                    "color_accuracy": (
                        round(color_accuracy, 5) if color_accuracy is not None else None
                    ),
                    "planned_passes": PLANNED_PASSES,
                    "identified_passes": identified_passes,
                    "detected_passes": detected_passes,
                    "missing_identified_passes": max(0, identified_passes - detected_passes),
                    "correct_passes": correct_passes,
                    "baseline_passes": len(baseline_pass_ids),
                    "missing_baseline_passes": len(missing_baseline_ids),
                    "extra_pass_ids": len(extra_pass_ids),
                    "plate_id_continuity": int(same_plate_ids),
                    "color_consistent_passes": color_consistent_passes,
                    "plate_color_consistency": (
                        round(
                            color_consistent_passes / len(baseline_pass_ids), 5
                        )
                        if baseline_pass_ids else None
                    ),
                    "range_qualified": int(range_qualified),
                    "continuous_range_qualified": int(continuous_range),
                    "pass_color_accuracy": (
                        round(pass_accuracy, 5) if pass_accuracy is not None else None
                    ),
                    "pass_success_vs_planned": round(correct_passes / PLANNED_PASSES, 5),
                    "orange_accuracy": (
                        round(per_color["orange"], 5)
                        if per_color["orange"] is not None else None
                    ),
                    "green_accuracy": (
                        round(per_color["light_green"], 5)
                        if per_color["light_green"] is not None else None
                    ),
                    "yellow_accuracy": (
                        round(per_color["yellow"], 5)
                        if per_color["yellow"] is not None else None
                    ),
                    "track_gap_count": gaps,
                    "track_gap_rate": round(gap_rate, 5),
                    "status": status,
                }
            )
            distance += BIN_SIZE_CM
        # A temporary missing bin is recoverable when the same persistent
        # pass_id reappears later with the correct plate color.  This preserves
        # end-to-end continuity while still exposing the blind interval.
        recoverable_continuity = bool(baseline_pass_ids)
        for index, metric in enumerate(metrics):
            bin_start = float(metric["distance_start_cm"])
            current_results = {
                int(row["pass_id"]): row
                for row in pass_results
                if float(row["distance_bin_start_cm"]) == bin_start
                and pass_observation_totals[int(row["pass_id"])]
                >= BASELINE_MIN_OBSERVATIONS
            }
            current_ids = set(current_results)
            missing_ids = baseline_pass_ids - current_ids
            extra_ids = current_ids - baseline_pass_ids
            recovered_ids = set()
            for missing_id in missing_ids:
                later = [
                    row for row in pass_results
                    if int(row["pass_id"]) == missing_id
                    and float(row["distance_bin_start_cm"]) > bin_start
                ]
                if later and all(color_ok(row) for row in later):
                    recovered_ids.add(missing_id)
            unrecovered_ids = missing_ids - recovered_ids
            present_colors_correct = all(
                color_ok(current_results[pass_id])
                for pass_id in baseline_pass_ids & current_ids
            )
            recoverable_ok = (
                bool(baseline_pass_ids)
                and not extra_ids
                and not unrecovered_ids
                and present_colors_correct
            )
            recoverable_continuity = recoverable_continuity and recoverable_ok
            metric["recovered_passes"] = len(recovered_ids)
            metric["recovered_pass_ids"] = " ".join(
                str(value) for value in sorted(recovered_ids)
            )
            metric["unrecovered_missing_passes"] = len(unrecovered_ids)
            metric["unrecovered_missing_pass_ids"] = " ".join(
                str(value) for value in sorted(unrecovered_ids)
            )
            metric["recoverable_range_qualified"] = int(recoverable_ok)
            metric["continuous_range_qualified"] = int(recoverable_continuity)
        return metrics

    def summary_lines(self) -> list[str]:
        metrics = self.metrics()
        stable = [row for row in metrics if row["status"] == "stable"]
        farthest = max((row["distance_end_cm"] for row in stable), default=None)
        unique_tracks = len({row["tracker_id"] for row in self.rows})
        result = [
            f"lap={self.current_lap_id} passes={len(self.pass_states)}/{PLANNED_PASSES} "
            f"observations={len(self.rows)} tracks={unique_tracks}",
            (
                "Q=quit R=ROI S=save continuous unknown test A=photo"
                if self.continuous_mode else
                "Q=quit R=ROI S=save B=start O-G-Y test A=photo"
            ),
            (
                "continuous=ACTIVE (unknown count/color)"
                if self.continuous_mode else
                "color test=ACTIVE (O>G>Y)"
                if self.evaluation_active else
                "color test=WAITING - press B before ORANGE enters"
            ),
            f"stable boundary={f'{farthest:.0f} cm' if farthest is not None else 'collecting'}",
        ]
        for row in reversed(metrics):
            if row["observations"] == 0:
                continue
            p10 = row["confidence_p10"]
            accuracy = row["pass_color_accuracy"]
            result.append(
                f"{row['distance_start_cm']:.0f}-{row['distance_end_cm']:.0f}cm "
                f"pass={row['correct_passes']}/{row['detected_passes']} p10={p10:.2f} "
                f"acc={accuracy:.1%} {row['status']}"
                if accuracy is not None else
                f"{row['distance_start_cm']:.0f}-{row['distance_end_cm']:.0f}cm "
                f"n={row['observations']} p10={p10:.2f} acc=-- {row['status']}"
            )
            if len(result) >= 7:
                break
        return result

    def analysis_markdown(self, metrics: list[dict], pass_results: list[dict]) -> str:
        """Build a readable analysis for the latest B-started evaluation cycle."""
        observations = len(self.rows)
        inference_times = {}
        for row in self.rows:
            inference_times.setdefault(
                int(row["inference_index"]), float(row["session_s"])
            )
        ordered_times = np.asarray(
            [inference_times[key] for key in sorted(inference_times)],
            dtype=float,
        )
        intervals = np.diff(ordered_times)
        valid_intervals = intervals[intervals > 0]
        mean_fps = (
            float(1.0 / np.mean(valid_intervals))
            if valid_intervals.size else None
        )
        median_fps = (
            float(1.0 / np.median(valid_intervals))
            if valid_intervals.size else None
        )
        duration = (
            float(ordered_times[-1] - ordered_times[0])
            if ordered_times.size >= 2 else 0.0
        )

        bin_counts = np.asarray(
            [int(row["observation_count"]) for row in pass_results],
            dtype=float,
        )
        mean_per_bin = float(np.mean(bin_counts)) if bin_counts.size else 0.0
        median_per_bin = float(np.median(bin_counts)) if bin_counts.size else 0.0

        pass_groups = defaultdict(list)
        for row in pass_results:
            pass_groups[int(row["pass_id"])].append(row)

        color_totals = Counter()
        color_correct = Counter()
        color_predictions = defaultdict(Counter)
        for row in self.rows:
            expected = str(row.get("expected_color", ""))
            predicted = str(row.get("predicted_color", "unknown"))
            if expected:
                color_totals[expected] += 1
                color_predictions[expected][predicted] += 1
                color_correct[expected] += int(predicted == expected)

        anomaly_counts = Counter()
        anomaly_rows = 0
        anomaly_file = self.directory / "anomaly_captures" / "anomalies.csv"
        first_inference = min(
            (int(row["inference_index"]) for row in self.rows),
            default=None,
        )
        if anomaly_file.exists():
            with anomaly_file.open("r", newline="", encoding="utf-8-sig") as handle:
                for anomaly in csv.DictReader(handle):
                    inference_index = int(anomaly.get("inference_index") or 0)
                    if first_inference is not None and inference_index < first_inference:
                        continue
                    anomaly_rows += 1
                    for reason in str(anomaly.get("reason", "")).split(";"):
                        if reason:
                            anomaly_counts[reason.split(":", 1)[0]] += 1

        identified_passes = len(self.pass_states)
        lap_ids = sorted(
            {
                int(row.get("lap_id") or 1)
                for row in pass_results
            }
        )

        lines = [
            "# 盤子距離自檢自動分析",
            "",
            f"產生時間：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "## 測試概況",
            "",
            "| 項目 | 結果 |",
            "|---|---:|",
            f"| 圈數 | {len(lap_ids)} |",
            f"| 盤次 | {identified_passes} / {PLANNED_PASSES} |",
            f"| observation | {observations} |",
            f"| 有效推論 frame | {len(inference_times)} |",
            f"| 記錄時間 | {duration:.2f} 秒 |",
            f"| 平均有效 FPS | {mean_fps:.2f} |" if mean_fps else "| 平均有效 FPS | 無法計算 |",
            f"| 中位有效 FPS | {median_fps:.2f} |" if median_fps else "| 中位有效 FPS | 無法計算 |",
            f"| 每盤每 10 cm 平均 observation | {mean_per_bin:.2f} |",
            f"| 每盤每 10 cm 中位 observation | {median_per_bin:.1f} |",
            "",
            "## 顏色結果",
            "",
            "| 預期顏色 | 正確 / 總數 | observation 正確率 | 預測分布 |",
            "|---|---:|---:|---|",
        ]
        for color in EXPECTED_COLOR_SEQUENCE:
            total = int(color_totals[color])
            correct = int(color_correct[color])
            accuracy = correct / total if total else None
            distribution = ", ".join(
                f"{name}={count}"
                for name, count in color_predictions[color].most_common()
            ) or "無資料"
            accuracy_text = f"{accuracy:.1%}" if accuracy is not None else "無資料"
            lines.append(
                f"| {color} | {correct} / {total} | {accuracy_text} | {distribution} |"
            )

        lines.extend([
            "",
            "## 每圈結果",
            "",
            "| Lap ID | Plate IDs | 盤數 | 正確區間 / 總區間 | 區間顏色正確率 |",
            "|---:|---|---:|---:|---:|",
        ])
        for lap_id in lap_ids:
            lap_rows = [
                row for row in pass_results
                if int(row.get("lap_id") or 1) == lap_id
            ]
            plate_ids = sorted({int(row["pass_id"]) for row in lap_rows})
            evaluated_lap_rows = [
                row for row in lap_rows if row.get("expected_color")
            ]
            correct_bins = sum(
                int(row["color_correct"]) for row in evaluated_lap_rows
            )
            total_bins = len(evaluated_lap_rows)
            accuracy_text = (
                f"{correct_bins / total_bins:.1%}" if total_bins else "無資料"
            )
            lines.append(
                f"| {lap_id} | {', '.join(map(str, plate_ids)) or '無'} "
                f"| {len(plate_ids)} | {correct_bins} / {total_bins} "
                f"| {accuracy_text} |"
            )

        lines.extend(["", "## 每個盤次", ""])
        for pass_id, rows in sorted(pass_groups.items()):
            lap_id = int(rows[0].get("lap_id") or 1)
            expected = str(rows[0]["expected_color"])
            evaluated_rows = [row for row in rows if row.get("expected_color")]
            bins = len(evaluated_rows)
            correct_bins = sum(int(row["color_correct"]) for row in evaluated_rows)
            first_cm = min(float(row["distance_bin_start_cm"]) for row in rows)
            last_cm = max(float(row["distance_bin_end_cm"]) for row in rows)
            lines.append(
                f"- L{lap_id} / P{pass_id} `{expected}`："
                f"{correct_bins}/{bins} 個區間正確，"
                f"觀測範圍 {first_cm:.0f}～{last_cm:.0f} cm。"
            )
        if not pass_groups:
            lines.append("- 尚未取得盤次資料。")

        lines.extend([
            "",
            "## 每 10 cm 結果",
            "",
            "| 距離 | observations | detected passes | correct passes | confidence p10 | confidence 中位數 | 狀態 |",
            "|---|---:|---:|---:|---:|---:|---|",
        ])
        for row in metrics:
            p10 = row["confidence_p10"]
            median = row["confidence_median"]
            lines.append(
                f"| {row['distance_start_cm']:.0f}～{row['distance_end_cm']:.0f} cm "
                f"| {row['observations']} | {row['detected_passes']} "
                f"| {row['correct_passes']} "
                f"| {p10:.3f}" if p10 is not None else
                f"| {row['distance_start_cm']:.0f}～{row['distance_end_cm']:.0f} cm "
                f"| {row['observations']} | {row['detected_passes']} "
                f"| {row['correct_passes']} | 無資料"
            )
            lines[-1] += (
                f" | {median:.3f} | {row['status']} |"
                if median is not None else
                f" | 無資料 | {row['status']} |"
            )

        range_rows = []
        pass_observation_totals = Counter()
        for result in pass_results:
            pass_observation_totals[int(result["pass_id"])] += int(
                result.get("observation_count", 0)
            )
        baseline_pass_ids = sorted(
            {
                int(row["pass_id"])
                for row in pass_results
                if float(row["distance_bin_start_cm"])
                == round(DETECTION_START_CM, 1)
                and pass_observation_totals[int(row["pass_id"])]
                >= BASELINE_MIN_OBSERVATIONS
            }
        )
        if identified_passes:
            for row in metrics:
                range_ok = bool(row["continuous_range_qualified"])
                range_rows.append((row, range_ok))

        qualified_range_rows = [item for item in range_rows if item[1]]
        provisional_end = max(
            (
                float(row["distance_end_cm"])
                for row, _ in qualified_range_rows
            ),
            default=None,
        )
        recommended_end = provisional_end
        first_failed_row = next(
            (row for row in metrics if not row["continuous_range_qualified"]),
            None,
        )

        lines.extend([
            "",
            "## 實際可辨識範圍判定",
            "",
            "判定規則：以 5～15 cm 出現的 pass_id 為基準。暫時漏失的 ID 若在後段 "
            "以相同 pass_id 且正確顏色重新出現，視為成功接回；只有未接回、出現額外 "
            "ID 或顏色錯誤才判定失敗。第一次不可恢復的失敗後停止延伸可辨識範圍。",
            "Observation 數與 confidence 只作為參考，不決定可辨識範圍。",
            "",
            "| 距離 | 出現 Plate ID | 缺少 ID | 多餘 ID | 後續可接回 ID | 本段接回 ID | ID 集合一致 | 顏色一致盤數 | 顏色一致率 | 恢復後範圍判定 |",
            "|---|---|---|---|---|---|---|---:|---:|---|",
        ])
        previous_missing_ids: set[int] = set()
        for row, range_ok in range_rows:
            bin_start = float(row["distance_start_cm"])
            present_ids = sorted(
                {
                    int(item["pass_id"])
                    for item in pass_results
                    if float(item["distance_bin_start_cm"]) == bin_start
                    and pass_observation_totals[int(item["pass_id"])]
                    >= BASELINE_MIN_OBSERVATIONS
                }
            )
            missing_ids = [
                pass_id for pass_id in baseline_pass_ids
                if pass_id not in present_ids
            ]
            extra_ids = [
                pass_id for pass_id in present_ids
                if pass_id not in baseline_pass_ids
            ]
            recovered_ids = sorted(previous_missing_ids & set(present_ids))
            present_text = ", ".join(map(str, present_ids)) or "無"
            missing_text = ", ".join(map(str, missing_ids)) or "無"
            extra_text = ", ".join(map(str, extra_ids)) or "無"
            recovered_text = ", ".join(map(str, recovered_ids)) or "無"
            future_recovered_text = (
                str(row.get("recovered_pass_ids", "")).replace(" ", ", ")
                or "無"
            )
            color_consistency = row["plate_color_consistency"]
            color_text = (
                f"{color_consistency:.1%}"
                if color_consistency is not None else "無資料"
            )
            lines.append(
                f"| {row['distance_start_cm']:.0f}～{row['distance_end_cm']:.0f} cm "
                f"| {present_text} | {missing_text} | {extra_text} "
                f"| {future_recovered_text} | {recovered_text} "
                f"| {'是' if row['plate_id_continuity'] else '否'} "
                f"| {row['color_consistent_passes']} | {color_text} "
                f"| {'合格' if range_ok else '不合格'} |"
            )
            previous_missing_ids = set(missing_ids)
        lines.append("")
        if provisional_end is None:
            lines.append("- 從第一個正式區間開始就沒有形成連續可辨識範圍。")
        else:
            lines.append(f"- 連續可辨識範圍：5～{recommended_end:.0f} cm。")
        if first_failed_row is not None:
            lines.append(
                f"- 第一次失敗區間：{first_failed_row['distance_start_cm']:.0f}～"
                f"{first_failed_row['distance_end_cm']:.0f} cm；"
                f"未接回漏盤 {first_failed_row['unrecovered_missing_passes']}、"
                f"額外 ID {first_failed_row['extra_pass_ids']}、"
                f"顏色一致 {first_failed_row['color_consistent_passes']}/"
                f"{first_failed_row['baseline_passes']} 盤。"
            )
        if identified_passes < PLANNED_PASSES:
            lines.append(
                f"- 目前只有 {identified_passes} 盤，尚未達規劃的 {PLANNED_PASSES} 盤；"
                "此結果只能作為暫定範圍，完成全部盤次後再定案。"
            )

        lines.extend([
            "",
            "## 異常截圖",
            "",
            f"本次評估週期共保存 {anomaly_rows} 張異常照片。",
            "",
        ])
        if anomaly_counts:
            for reason, count in anomaly_counts.most_common():
                lines.append(f"- `{reason}`：{count}")
        else:
            lines.append("- 沒有異常事件，或尚未產生異常 metadata。")

        lines.extend(["", "## 自動判讀", ""])
        if observations == 0:
            lines.append("- 尚無有效 observation，不能判斷距離或顏色表現。")
        else:
            if recommended_end is not None:
                lines.append(
                    f"- 依固定 pass_id、暫時漏失後可用同一 ID 接回、無額外 ID "
                    f"與盤級顏色一致性判定，"
                    f"連續可辨識範圍為 5～{recommended_end:.0f} cm。"
                )
            else:
                lines.append("- 目前沒有形成連續可辨識範圍。")
            for color in EXPECTED_COLOR_SEQUENCE:
                total = int(color_totals[color])
                if not total:
                    lines.append(f"- `{color}` 沒有資料。")
                    continue
                accuracy = color_correct[color] / total
                lines.append(
                    f"- `{color}` observation 正確率為 {accuracy:.1%}。"
                )
            if anomaly_counts["wrong_color"]:
                lines.append(
                    "- 主要異常包含錯色；應先確認顏色分類使用的影像與色域，再依此批資料調整 ROI。"
                )
            if anomaly_counts["low_confidence"]:
                lines.append(
                    "- 有低信心事件；請查看 anomaly_captures 中相同編號的 annotated 與 processed 圖片。"
                )
            lines.append(
                "- 這是自動摘要；正式偽陽率仍需要有明確標記的空軌道 frame 作為分母。"
            )

        lines.extend([
            "",
            "## 資料檔案",
            "",
            "- `distance_detections.csv`：逐次 observation",
            "- `pass_distance_results.csv`：每盤每 10 cm 多數決",
            "- `distance_metrics.csv`：每 10 cm 彙總",
            "- `anomaly_captures/anomalies.csv`：異常照片索引",
            "- `color_calibration.csv`：逐 observation HLS／Lab 量測",
            "- `color_calibration_suggestions.md`：自動色域建議",
            "",
        ])
        return "\n".join(lines)

    def color_calibration_markdown(self) -> str:
        lines = [
            "# 顏色校準建議",
            "",
            f"產生時間：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "資料來源為本次 B 啟動後、前處理影像中 segmentation 內縮區域的有效彩色像素。",
            "建議值不會自動寫入正式 detector；請先用另一批盤次驗證。",
            "",
            "## 實測分布與建議",
            "",
            "| 顏色 | observations | H p10 | H 中位數 | H p90 | 建議 HLS 範圍 | 建議 Lab (a,b) |",
            "|---|---:|---:|---:|---:|---|---|",
        ]
        suggested_hues = {}
        lab_centers = {}
        for color in EXPECTED_COLOR_SEQUENCE:
            rows = [
                row for row in self.color_samples
                if row["expected_color"] == color
            ]
            if not rows:
                lines.append(f"| {color} | 0 | - | - | - | 無資料 | 無資料 |")
                continue
            hues = np.asarray([float(row["h_median"]) for row in rows])
            if color == "orange":
                hues = np.where(hues > 150.0, hues - 180.0, hues)
            h10, h50, h90 = np.percentile(hues, [10, 50, 90])
            low = int(math.floor(h10 - 2.0))
            high = int(math.ceil(h90 + 2.0))
            hue_values = set()
            if low < 0:
                ranges = [(0, min(180, high)), (max(0, 180 + low), 180)]
            elif high > 180:
                ranges = [(max(0, low), 180), (0, min(180, high - 180))]
            else:
                ranges = [(max(0, low), min(180, high))]
            for range_low, range_high in ranges:
                hue_values.update(range(range_low, range_high + 1))
            suggested_hues[color] = hue_values
            range_text = ", ".join(f"({a}, {b})" for a, b in ranges)
            lab_a = float(np.median([row["lab_a_median"] for row in rows]))
            lab_b = float(np.median([row["lab_b_median"] for row in rows]))
            lab_centers[color] = (lab_a, lab_b)
            lines.append(
                f"| {color} | {len(rows)} | {h10:.1f} | {h50:.1f} | {h90:.1f} "
                f"| `{range_text}` | `({lab_a:.1f}, {lab_b:.1f})` |"
            )

        lines.extend(["", "## 範圍重疊檢查", ""])
        overlaps = []
        colors = list(suggested_hues)
        for index, left in enumerate(colors):
            for right in colors[index + 1:]:
                shared = sorted(suggested_hues[left] & suggested_hues[right])
                if shared:
                    overlaps.append((left, right, shared))
        if overlaps:
            for left, right, shared in overlaps:
                lines.append(
                    f"- `{left}` 與 `{right}` 重疊 H={min(shared)}～{max(shared)}；"
                    "不要直接套用，需增加樣本或用 Lab 作邊界判斷。"
                )
        else:
            lines.append("- 三種顏色的建議 HLS 範圍沒有重疊。")

        lines.extend([
            "",
            "## 建議常數",
            "",
            "```python",
            "HLS_HUE_RANGES = {",
        ])
        for color in EXPECTED_COLOR_SEQUENCE:
            if color not in suggested_hues:
                continue
            values = sorted(suggested_hues[color])
            ranges = []
            start = previous = values[0]
            for value in values[1:]:
                if value != previous + 1:
                    ranges.append((start, previous))
                    start = value
                previous = value
            ranges.append((start, previous))
            lines.append(f'    "{color}": {ranges},')
        lines.extend(["}", "", "LAB_COLOR_CENTERS = {"])
        for color in EXPECTED_COLOR_SEQUENCE:
            if color in lab_centers:
                a_value, b_value = lab_centers[color]
                lines.append(
                    f'    "{color}": ({a_value:.1f}, {b_value:.1f}),' 
                )
        lines.extend([
            "}",
            "```",
            "",
            "## 驗證要求",
            "",
            "- 每種顏色建議至少累積 20 個完整盤次後再採用。",
            "- 校準資料與驗證資料必須分開。",
            "- 驗證時要逐 10 cm 檢查正確率與橘黃、綠黃混淆。",
            "",
        ])
        return "\n".join(lines)

    def save(self) -> None:
        detection_fields = [
            "session_s",
            "wall_timestamp",
            "inference_index",
            "tracker_id",
            "pass_id",
            "lap_id",
            "distance_cm",
            "distance_bin_start_cm",
            "distance_bin_end_cm",
            "confidence",
            "predicted_color",
            "current_color",
            "raw_predicted_color",
            "raw_current_color",
            "expected_color",
            "upstream_pass_id",
            "upstream_sequence",
            "upstream_published_at",
            "mqtt_received_at",
            "color_correct",
        ]
        with (self.directory / "distance_detections.csv").open(
            "w", newline="", encoding="utf-8-sig"
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=detection_fields)
            writer.writeheader()
            writer.writerows(self.rows)

        color_event_fields = [
            "wall_timestamp", "session_s", "inference_index", "lap_id",
            "pass_id", "tracker_id", "distance_cm", "confidence",
            "current_color", "stable_color", "raw_current_color",
            "raw_stable_color", "upstream_pass_id",
            "upstream_sequence", "upstream_color", "upstream_published_at",
            "mqtt_received_at", "mqtt_delay_ms",
        ]
        with (self.directory / "downstream_color_events.csv").open(
            "w", newline="", encoding="utf-8-sig"
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=color_event_fields)
            writer.writeheader()
            for row in self.rows:
                published_at = row.get("upstream_published_at", "")
                received_at = row.get("mqtt_received_at", "")
                mqtt_delay_ms = ""
                if published_at not in ("", None) and received_at not in ("", None):
                    mqtt_delay_ms = round(
                        (float(received_at) - float(published_at)) * 1000.0, 3
                    )
                writer.writerow({
                    "wall_timestamp": row.get("wall_timestamp", ""),
                    "session_s": row.get("session_s", ""),
                    "inference_index": row.get("inference_index", ""),
                    "lap_id": row.get("lap_id", ""),
                    "pass_id": row.get("pass_id", ""),
                    "tracker_id": row.get("tracker_id", ""),
                    "distance_cm": row.get("distance_cm", ""),
                    "confidence": row.get("confidence", ""),
                    "current_color": row.get("current_color", ""),
                    "stable_color": row.get("predicted_color", ""),
                    "raw_current_color": row.get("raw_current_color", ""),
                    "raw_stable_color": row.get("raw_predicted_color", ""),
                    "upstream_pass_id": row.get("upstream_pass_id", ""),
                    "upstream_sequence": row.get("upstream_sequence", ""),
                    "upstream_color": row.get("expected_color", ""),
                    "upstream_published_at": published_at,
                    "mqtt_received_at": received_at,
                    "mqtt_delay_ms": mqtt_delay_ms,
                })

        color_summary_fields = [
            "pass_id", "lap_id", "tracker_ids", "observation_count",
            "first_seen_at", "last_seen_at", "first_stable_color",
            "majority_color", "final_color", "color_change_count",
            "upstream_pass_id", "upstream_sequence", "upstream_color",
            "mqtt_delay_ms", "reference_vs_first_detection_ms",
        ]
        with (self.directory / "downstream_color_summary.csv").open(
            "w", newline="", encoding="utf-8-sig"
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=color_summary_fields)
            writer.writeheader()
            grouped_rows = defaultdict(list)
            for row in self.rows:
                grouped_rows[int(row["pass_id"])].append(row)
            for pass_id, rows in sorted(grouped_rows.items()):
                rows = sorted(rows, key=lambda item: float(item["wall_timestamp"]))
                colors = [str(row.get("predicted_color", "unknown")) for row in rows]
                known_colors = [color for color in colors if color != "unknown"]
                majority_color = (
                    Counter(known_colors).most_common(1)[0][0]
                    if known_colors else "unknown"
                )
                color_changes = sum(
                    left != right
                    for left, right in zip(known_colors, known_colors[1:])
                )
                state = self.pass_states.get(pass_id, {})
                published_at = state.get("upstream_published_at")
                received_at = state.get("mqtt_received_at")
                first_seen_at = float(rows[0]["wall_timestamp"])
                writer.writerow({
                    "pass_id": pass_id,
                    "lap_id": rows[0].get("lap_id", ""),
                    "tracker_ids": " ".join(
                        str(value) for value in sorted(
                            {int(row["tracker_id"]) for row in rows}
                        )
                    ),
                    "observation_count": len(rows),
                    "first_seen_at": first_seen_at,
                    "last_seen_at": rows[-1].get("wall_timestamp", ""),
                    "first_stable_color": known_colors[0] if known_colors else "unknown",
                    "majority_color": majority_color,
                    "final_color": state.get("final_color") or majority_color,
                    "color_change_count": color_changes,
                    "upstream_pass_id": state.get("upstream_pass_id", ""),
                    "upstream_sequence": state.get("upstream_sequence", ""),
                    "upstream_color": state.get("expected_color", ""),
                    "mqtt_delay_ms": (
                        round((float(received_at) - float(published_at)) * 1000.0, 3)
                        if received_at is not None and published_at is not None else ""
                    ),
                    "reference_vs_first_detection_ms": (
                        round((float(received_at) - first_seen_at) * 1000.0, 3)
                        if received_at is not None else ""
                    ),
                })

        calibration_fields = [
            "inference_index", "tracker_id", "pass_id", "lap_id", "distance_cm",
            "distance_bin_start_cm", "expected_color", "predicted_color",
            "confidence", "sample_pixels", "h_p10", "h_median", "h_p90",
            "l_median", "s_median", "lab_a_median", "lab_b_median",
        ]
        with (self.directory / "color_calibration.csv").open(
            "w", newline="", encoding="utf-8-sig"
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=calibration_fields)
            writer.writeheader()
            writer.writerows(self.color_samples)

        pass_results = self.pass_bin_results()
        pass_fields = [
            "pass_id", "lap_id", "expected_color", "distance_bin_start_cm",
            "distance_bin_end_cm", "observation_count", "majority_color",
            "majority_count", "majority_ratio", "color_correct",
            "confidence_median", "tracker_ids",
        ]
        with (self.directory / "pass_distance_results.csv").open(
            "w", newline="", encoding="utf-8-sig"
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=pass_fields)
            writer.writeheader()
            writer.writerows(pass_results)

        metrics = self.metrics()
        with (self.directory / "distance_metrics.csv").open(
            "w", newline="", encoding="utf-8-sig"
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=list(metrics[0].keys()))
            writer.writeheader()
            writer.writerows(metrics)

        with (self.directory / "distance_summary.json").open(
            "w", encoding="utf-8"
        ) as handle:
            json.dump(
                {
                    "track_length_cm": TRACK_LENGTH_CM,
                    "required_detection_length_cm": REQUIRED_DETECTION_LENGTH_CM,
                    "entry_buffer_cm": ENTRY_BUFFER_CM,
                    "detection_start_cm": DETECTION_START_CM,
                    "detection_end_cm": DETECTION_END_CM,
                    "exit_buffer_cm": EXIT_BUFFER_CM,
                    "bin_size_cm": BIN_SIZE_CM,
                    "planned_passes": PLANNED_PASSES,
                    "identified_passes": len(self.pass_states),
                    "recorded_laps": self.current_lap_id,
                    "thresholds": {
                        "minimum_observations_per_bin": MIN_OBSERVATIONS_PER_BIN,
                        "minimum_confidence_p10": MIN_CONFIDENCE_P10,
                        "maximum_track_gap_rate": MAX_TRACK_GAP_RATE,
                        "minimum_color_accuracy": MIN_COLOR_ACCURACY,
                    },
                    "metric_scope": "pass_distance_majority_vote_and_tracking_stability",
                    "color_accuracy_scope": (
                        "conditional_on_detected_observations_with_O_G_Y_sequence"
                    ),
                    "metrics": metrics,
                },
                handle,
                ensure_ascii=False,
                indent=2,
            )
        (self.directory / "analysis_summary.md").write_text(
            self.analysis_markdown(metrics, pass_results),
            encoding="utf-8",
        )
        (self.directory / "color_calibration_suggestions.md").write_text(
            self.color_calibration_markdown(),
            encoding="utf-8",
        )
        print("Self-check saved:", self.directory)

    def reset(self) -> None:
        self.rows.clear()
        self.by_bin.clear()
        self.bin_tracker_ids.clear()
        self.last_seen_inference.clear()
        self.tracker_gaps.clear()
        self.tracker_observations.clear()
        self.color_samples.clear()
        self.tracker_to_pass.clear()
        self.pass_states.clear()
        self.next_pass_id = 1
        self.next_expected_index = 0
        self.current_lap_id = 0
        self.evaluation_active = False
        self.started = time.perf_counter()
        print("Self-check statistics reset.")


class AnomalyCapture:
    """Save annotated and processed evidence for suspicious frames."""

    def __init__(self, session_directory: Path):
        self.directory = session_directory / "anomaly_captures"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.metadata_file = self.directory / "anomalies.csv"
        self.seen_trackers: set[int] = set()
        self.last_color_by_pass: dict[int, str] = {}
        self.last_automatic_save = -1e9
        self.capture_count = 0

    def reset_tracking_state(self) -> None:
        self.seen_trackers.clear()
        self.last_color_by_pass.clear()
        self.last_automatic_save = -1e9

    @staticmethod
    def _instance_summary(instances: list[dict]) -> str:
        summary = [
            {
                "pass_id": instance.get("pass_id"),
                "tracker_id": int(instance["tracker_id"]),
                "distance_cm": round(float(instance.get("distance_cm", 0.0)), 2),
                "confidence": round(float(instance["confidence"]), 4),
                "color": str(instance.get("stable_color", "unknown")),
            }
            for instance in instances
        ]
        return json.dumps(summary, ensure_ascii=False, separators=(",", ":"))

    def _save(
        self,
        reason: str,
        inference_index: int,
        annotated_frame: np.ndarray,
        model_input: np.ndarray,
        instances: list[dict],
    ) -> None:
        self.capture_count += 1
        stamp = time.strftime("%Y%m%d_%H%M%S")
        base = f"{stamp}_{self.capture_count:04d}_f{inference_index:06d}"
        annotated_name = f"{base}_annotated.png"
        processed_name = f"{base}_processed.png"
        annotated_path = self.directory / annotated_name
        processed_path = self.directory / processed_name
        if not cv2.imwrite(str(annotated_path), annotated_frame):
            raise RuntimeError(f"Cannot save anomaly image: {annotated_path}")
        if not cv2.imwrite(str(processed_path), model_input):
            raise RuntimeError(f"Cannot save anomaly image: {processed_path}")

        write_header = not self.metadata_file.exists()
        with self.metadata_file.open("a", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "capture_index", "inference_index", "reason",
                    "annotated_image", "processed_image",
                    "detection_count", "instances",
                ],
            )
            if write_header:
                writer.writeheader()
            writer.writerow(
                {
                    "capture_index": self.capture_count,
                    "inference_index": inference_index,
                    "reason": reason,
                    "annotated_image": annotated_name,
                    "processed_image": processed_name,
                    "detection_count": len(instances),
                    "instances": self._instance_summary(instances),
                }
            )
        print(f"Anomaly capture saved: {base} reason={reason}")

    def observe(
        self,
        inference_index: int,
        annotated_frame: np.ndarray,
        model_input: np.ndarray,
        instances: list[dict],
        evaluation_active: bool,
    ) -> None:
        if not evaluation_active:
            return

        reasons = []
        has_new_tracker = False
        for instance in instances:
            tracker_id = int(instance["tracker_id"])
            confidence = float(instance["confidence"])
            color = str(instance.get("stable_color", "unknown"))
            pass_id = instance.get("pass_id")
            expected_color = str(instance.get("expected_color", ""))
            if tracker_id not in self.seen_trackers:
                self.seen_trackers.add(tracker_id)
                reasons.append(f"new_tracker:T{tracker_id}")
                has_new_tracker = True
            if confidence < ANOMALY_LOW_CONFIDENCE:
                reasons.append(f"low_confidence:{confidence:.2f}")
            if expected_color and color != expected_color:
                reasons.append(
                    f"wrong_color:{expected_color}_to_{color}"
                )
            if pass_id is not None:
                pass_id = int(pass_id)
                previous_color = self.last_color_by_pass.get(pass_id)
                if previous_color is not None and color != previous_color:
                    reasons.append(
                        f"color_change:P{pass_id}:{previous_color}_to_{color}"
                    )
                self.last_color_by_pass[pass_id] = color

        if len(instances) > ANOMALY_MAX_EXPECTED_INSTANCES:
            reasons.append(f"too_many_detections:{len(instances)}")
        if not reasons:
            return

        now = time.perf_counter()
        if (
            not has_new_tracker
            and now - self.last_automatic_save
            < ANOMALY_CAPTURE_COOLDOWN_SECONDS
        ):
            return
        self.last_automatic_save = now
        self._save(
            ";".join(dict.fromkeys(reasons)), inference_index,
            annotated_frame, model_input, instances,
        )

    def save_manual(
        self,
        inference_index: int,
        annotated_frame: np.ndarray,
        model_input: np.ndarray,
        instances: list[dict],
    ) -> None:
        self._save(
            "manual:A_key", inference_index,
            annotated_frame, model_input, instances,
        )


def draw_panel(frame: np.ndarray, lines: list[str]) -> None:
    x, y = 12, 24
    width = min(frame.shape[1] - 24, 520)
    height = 18 + len(lines) * 22
    overlay = frame.copy()
    cv2.rectangle(overlay, (6, 6), (6 + width, 6 + height), (15, 15, 15), -1)
    cv2.addWeighted(overlay, 0.72, frame, 0.28, 0, frame)
    for line in lines:
        cv2.putText(
            frame, line, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.48,
            (245, 245, 245), 1, cv2.LINE_AA,
        )
        y += 22


def draw_waiting_banner(frame: np.ndarray, blocked: bool = False) -> None:
    height, width = frame.shape[:2]
    overlay = frame.copy()
    top = max(70, height // 2 - 75)
    bottom = min(height - 20, top + 150)
    cv2.rectangle(overlay, (20, top), (width - 20, bottom), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.78, frame, 0.22, 0, frame)
    title = "CLEAR ROI FIRST" if blocked else "TEST NOT RECORDING"
    subtitle = (
        "Plate detected - wait until ROI is empty"
        if blocked else
        "Press B before the ORANGE plate enters (O > G > Y)"
    )
    title_size = cv2.getTextSize(
        title, cv2.FONT_HERSHEY_SIMPLEX, 1.05, 3
    )[0]
    subtitle_size = cv2.getTextSize(
        subtitle, cv2.FONT_HERSHEY_SIMPLEX, 0.65, 2
    )[0]
    cv2.putText(
        frame, title, ((width - title_size[0]) // 2, top + 55),
        cv2.FONT_HERSHEY_SIMPLEX, 1.05, (0, 80, 255), 3, cv2.LINE_AA,
    )
    cv2.putText(
        frame, subtitle, ((width - subtitle_size[0]) // 2, top + 105),
        cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 230, 255), 2, cv2.LINE_AA,
    )


def main() -> None:
    args = parse_args()
    if args.list_cameras:
        list_cameras()
        return

    saved_binding = None
    logical_camera_id = args.camera_id.strip()
    if logical_camera_id:
        if args.camera is None:
            args.camera, saved_binding = resolve_camera_index(logical_camera_id)
            print(
                f"Resolved {logical_camera_id} to current camera index "
                f"{args.camera} via saved USB path."
            )
        else:
            saved_binding = binding_for(logical_camera_id)
    if args.camera is None:
        args.camera = DEFAULT_CAMERA_INDEX
    if saved_binding is not None:
        if not args.mqtt_camera_id.strip():
            args.mqtt_camera_id = str(saved_binding.get("mqtt_camera_id", ""))
        if args.handoff_role is None:
            args.handoff_role = str(saved_binding.get("handoff_role", "off"))
        if not args.peer_camera_id.strip():
            args.peer_camera_id = str(saved_binding.get("peer_camera_id", ""))
    if args.handoff_role is None:
        args.handoff_role = "off"

    api_key = str(const.INFERENCE_API_KEY).strip()
    if not api_key:
        raise RuntimeError("INFERENCE_API_KEY is empty in the existing detector settings.")

    client = InferenceHTTPClient(api_url=str(const.INFERENCE_URL), api_key=api_key)
    client.configure(
        InferenceConfiguration(
            confidence_threshold=CONFIDENCE_THRESHOLD,
            iou_threshold=IOU_THRESHOLD,
            api_key_transport="header",
        )
    )
    tracker = sv.ByteTrack(
        track_activation_threshold=CONFIDENCE_THRESHOLD,
        lost_track_buffer=det.TRACK_LOST_BUFFER,
        minimum_matching_threshold=det.TRACK_MINIMUM_MATCHING_THRESHOLD,
        frame_rate=max(1.0, float(det.CAMERA_FPS)),
        minimum_consecutive_frames=det.TRACK_MINIMUM_CONSECUTIVE_FRAMES,
    )
    color_history = defaultdict(lambda: deque(maxlen=det.COLOR_HISTORY_LENGTH))
    capture, frame = open_camera(args.camera)

    aoi_polygon = load_self_check_aoi(frame.shape)
    if args.select_roi or aoi_polygon is None:
        if args.headless:
            raise RuntimeError(
                "Headless mode requires an existing self_check_aoi.json; "
                "run once with --select-roi before deployment."
            )
        aoi_polygon = select_self_check_aoi(frame)
    image_to_world, world_to_image = build_world_transform(aoi_polygon)
    role = args.handoff_role
    camera_id = logical_camera_id
    mqtt_camera_id = args.mqtt_camera_id.strip() or camera_id
    peer_camera_id = args.peer_camera_id.strip()
    if args.headless and role != "off":
        args.continuous_handoff = True
    if role != "off" and (not mqtt_camera_id or not peer_camera_id):
        raise RuntimeError(
            "a camera/MQTT ID and --peer-camera-id are required when handoff is enabled"
        )
    if args.continuous_handoff and role == "off":
        raise RuntimeError(
            "--continuous-handoff requires --handoff-role upstream or downstream"
        )
    recorder = SelfCheckRecorder(
        external_expected=(role == "downstream"),
        session_suffix=camera_id or mqtt_camera_id,
        continuous_mode=bool(args.continuous_handoff),
    )
    handoff = None
    if role != "off":
        handoff = HandoffMQTT(
            role=role,
            camera_id=mqtt_camera_id,
            peer_camera_id=peer_camera_id,
            output_directory=recorder.directory,
            host=args.mqtt_host,
            port=args.mqtt_port,
            username=str(const.REPORT_MQTT_USERNAME),
            password=str(const.REPORT_MQTT_PASSWORD),
            qos=int(const.REPORT_MQTT_QOS),
            matching_mode=args.matching_mode,
        )
        recorder.handoff = handoff
        handoff.start()
    anomaly_capture = AnomalyCapture(recorder.directory)
    inference_index = 0
    last_autosave = time.monotonic()

    if args.headless:
        print(
            f"Distance self-check production mode started on camera {args.camera}; "
            f"autosave={args.autosave_seconds:g}s; no keyboard input required."
        )
    elif args.continuous_handoff:
        print(
            f"Continuous handoff started automatically on camera {args.camera}; "
            f"autosave={args.autosave_seconds:g}s. No B or Q required; "
            "use Ctrl+C in the supervisor terminal to stop both cameras."
        )
    else:
        print(
            f"Distance self-check started on camera {args.camera}. "
            "Q=quit, R=redraw ROI, S=save report, B=start test, "
            "A=save abnormal image"
        )
    if args.continuous_handoff:
        if args.headless:
            print("Continuous handoff ACTIVE: automatic start and persistence.")
        else:
            print("Continuous handoff ACTIVE: recording now; unknown count/color.")
    print("Output:", recorder.directory)
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            original_aoi, masked_aoi, aoi_x, aoi_y, inverse, _warped = (
                det.prepare_aoi_view(frame, aoi_polygon, det.DEFAULT_WARP_ENABLED)
            )
            model_input = det.preprocess_aoi_for_inference(masked_aoi)
            result = det.run_inference(client, model_input)
            instances = det.process_predictions(
                result=result,
                # Match production: inference and color/reflection filtering
                # use the same reference-calibrated brightness and contrast.
                original_aoi=model_input,
                tracker=tracker,
                color_history=color_history,
            )
            for instance in instances:
                normalize_operational_colors(instance)
            inference_index += 1
            for instance in instances:
                polygon = det.project_polygon_to_frame(
                    instance["polygon"], aoi_x, aoi_y, frame.shape, inverse
                )
                center = np.mean(polygon.astype(np.float32), axis=0)
                world = transform_points(center.reshape(1, 2), image_to_world)[0]
                instance["distance_cm"] = float(
                    np.clip(world[1], NEAR_DISTANCE_CM, TRACK_LENGTH_CM)
                )
                instance["projected_polygon"] = polygon

            recorder.record(inference_index, instances, model_input)
            if handoff is not None:
                handoff.tick()
            output = frame.copy()
            det.draw_aoi_overlay(output, aoi_polygon)
            draw_distance_grid(output, world_to_image)
            for instance in instances:
                polygon = instance["projected_polygon"]
                color = det.color_bgr(instance.get("stable_color", "unknown"))
                box_x1 = int(np.min(polygon[:, 0]))
                box_y1 = int(np.min(polygon[:, 1]))
                box_x2 = int(np.max(polygon[:, 0]))
                box_y2 = int(np.max(polygon[:, 1]))
                cv2.rectangle(
                    output,
                    (box_x1, box_y1),
                    (box_x2, box_y2),
                    color,
                    2,
                    cv2.LINE_AA,
                )
                label = (
                    f"P{instance.get('pass_id', '-')}/T{instance['tracker_id']} "
                    f"{instance['distance_cm']:.1f}cm "
                    f"C{instance['confidence']:.2f} "
                    f"{instance.get('measurement_phase', '')}"
                )
                cv2.putText(
                    output, label, (box_x1, max(18, box_y1 - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.46, color, 2, cv2.LINE_AA,
                )
            panel_lines = recorder.summary_lines()
            if handoff is not None:
                panel_lines.extend(handoff.display_lines())
            draw_panel(output, panel_lines)
            anomaly_capture.observe(
                inference_index=inference_index,
                annotated_frame=output,
                model_input=model_input,
                instances=instances,
                evaluation_active=recorder.evaluation_active,
            )
            cv2.putText(
                output,
                f"A=save anomaly | saved={anomaly_capture.capture_count}",
                (12, output.shape[0] - 14),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.48,
                (0, 220, 255),
                1,
                cv2.LINE_AA,
            )
            if not recorder.evaluation_active:
                draw_waiting_banner(output, blocked=bool(instances))
            if (
                args.autosave_seconds > 0
                and time.monotonic() - last_autosave >= args.autosave_seconds
            ):
                recorder.save()
                if handoff is not None:
                    handoff.snapshot()
                last_autosave = time.monotonic()
            if args.headless:
                key = -1
            else:
                cv2.imshow(WINDOW_TITLE, output)
                key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            if key == ord("s"):
                recorder.save()
                if handoff is not None:
                    handoff.save()
            if key == ord("a"):
                anomaly_capture.save_manual(
                    inference_index=inference_index,
                    annotated_frame=output,
                    model_input=model_input,
                    instances=instances,
                )
            if key == ord("b"):
                if args.continuous_handoff:
                    print("B ignored: continuous handoff is already recording.")
                    continue
                if instances:
                    print(
                        "Cannot start: a plate/detection is already inside ROI. "
                        "Wait until ROI is empty, then press B before ORANGE enters."
                    )
                else:
                    tracker.reset()
                    color_history.clear()
                    recorder.start_color_cycle()
                    anomaly_capture.reset_tracking_state()
            if key == ord("r"):
                aoi_polygon = select_self_check_aoi(frame)
                image_to_world, world_to_image = build_world_transform(aoi_polygon)
                tracker.reset()
                color_history.clear()
                recorder.reset()
                anomaly_capture.reset_tracking_state()
    finally:
        recorder.save()
        if handoff is not None:
            handoff.stop()
        capture.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
