"""Create and inspect persistent USB-port camera bindings."""

from __future__ import annotations

import argparse

from plate_detector.camera_registry import (
    enumerate_devices,
    load_bindings,
    resolve_camera_index,
    save_binding,
)


def bind_main() -> None:
    parser = argparse.ArgumentParser(description="Bind a logical ID to a USB camera path")
    parser.add_argument("--camera", type=int, required=True, help="current camera index")
    parser.add_argument("--camera-id", required=True, help="stable logical ID")
    parser.add_argument("--mqtt-camera-id", default="", help="MQTT ID; defaults to camera-id")
    parser.add_argument(
        "--handoff-role", choices=("off", "upstream", "downstream"), default="off"
    )
    parser.add_argument("--peer-camera-id", default="")
    args = parser.parse_args()
    binding = save_binding(
        camera_index=args.camera,
        camera_id=args.camera_id,
        mqtt_camera_id=args.mqtt_camera_id,
        handoff_role=args.handoff_role,
        peer_camera_id=args.peer_camera_id,
    )
    print("Camera binding saved:")
    for key, value in binding.items():
        print(f"  {key}: {value}")


def list_main() -> None:
    parser = argparse.ArgumentParser(description="List saved and currently resolved cameras")
    parser.parse_args()
    data = load_bindings()
    if not data["bindings"]:
        print("No camera bindings. Use: python main.py camera-bind --camera N --camera-id ID")
        return
    devices = enumerate_devices()
    for binding in data["bindings"]:
        camera_id = str(binding["camera_id"])
        try:
            index, _resolved = resolve_camera_index(camera_id, devices=devices)
            status = f"connected as index {index}"
        except LookupError as error:
            status = f"unavailable ({error})"
        print(
            f"{camera_id}: {status}; mqtt={binding.get('mqtt_camera_id', camera_id)}; "
            f"role={binding.get('handoff_role', 'off')}; "
            f"device={binding.get('device_name', '')}"
        )
