"""Application settings.

Edit this file for the deployment environment. Keep real API keys and MQTT
passwords out of commits made to a public repository.
"""

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def project_path(value: str | Path) -> Path:
    """Resolve a setting path relative to the project directory."""
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path

# Camera
CAMERA_INDEX = 2
CAMERA_SOURCE = ""
CAMERA_WIDTH = 1280
CAMERA_HEIGHT = 720
CAMERA_FPS = 30

# Inference
INFERENCE_URL = "http://127.0.0.1:9140"
INFERENCE_API_KEY = os.environ.get("PLATE_INFERENCE_API_KEY", "")
INFERENCE_MODEL_ID = "-v4qph/-j7pjz-4-yolo26n-seg-t1"
INFERENCE_TARGET_CLASS = ""

# AOI
AOI_ENABLED = True
AOI_FILE = "detector_config/cam-2-aoi.json"
AOI_WARP_ENABLED = False
AOI_WARP_STRENGTH = 0.35

# Detection and recording
DETECTION_EXCLUDED_COLORS = ["black"]
DETECTION_CAPTURE_DETECTIONS = True
DETECTION_CAPTURE_ROOT = "false_positive_captures/cam-2"
DETECTION_CAPTURE_INTERVAL_SECONDS = 1.0
DETECTION_OUTPUT_VIDEO = ""
RECORDING_ENABLED = True
RECORDING_ROOT = "outputs/cam-2/bbox_accuracy"

# MQTT
MQTT_ENABLED = True
REPORT_MQTT_BROKER_HOST = "127.0.0.1"
REPORT_MQTT_BROKER_PORT = 1883
REPORT_MQTT_USERNAME = os.environ.get("PLATE_MQTT_USERNAME", "")
REPORT_MQTT_PASSWORD = os.environ.get("PLATE_MQTT_PASSWORD", "")
REPORT_MQTT_CLIENT_ID = "plate-detector"
REPORT_MQTT_QOS = 1
MQTT_KEEPALIVE = 30
MQTT_CAMERA_ID = "cam-2"
MQTT_DISAPPEAR_CONFIRM_SECONDS = 2.3
MQTT_EXIT_APPROACH_RATIO = 0.35

# Accuracy test metadata
TEST_MEAL = "unspecified"
TEST_SPACING = "unspecified"
TEST_SPEED = "unspecified"
TEST_PLANNED_LAPS = 10


