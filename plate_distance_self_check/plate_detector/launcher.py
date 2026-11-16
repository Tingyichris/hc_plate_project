"""Launch the production detector with optional USB camera binding resolution."""

from __future__ import annotations

import argparse

from . import constants as const
from .camera_registry import resolve_camera_index


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--camera", type=int, help="explicit current camera index")
    selection.add_argument("--camera-id", default="", help="saved logical camera ID")
    parser.add_argument(
        "--mqtt-camera-id",
        default="",
        help="override the binding/default MQTT camera ID",
    )
    args = parser.parse_args()

    camera_index = args.camera
    mqtt_camera_id = args.mqtt_camera_id.strip()
    if args.camera_id:
        camera_index, binding = resolve_camera_index(args.camera_id)
        if not mqtt_camera_id:
            mqtt_camera_id = str(binding.get("mqtt_camera_id", "")).strip()
        print(
            f"Resolved {args.camera_id} to current camera index {camera_index} "
            "via saved USB path."
        )
    from . import detector

    if camera_index is not None:
        detector.CAMERA_ID = int(camera_index)
        detector.VIDEO_SOURCE = None
    if mqtt_camera_id:
        const.MQTT_CAMERA_ID = mqtt_camera_id
    detector.main()


if __name__ == "__main__":
    main()
