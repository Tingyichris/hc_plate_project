"""Persistent logical-camera bindings resolved from stable USB device paths."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterable

import cv2
from cv2_enumerate_cameras import enumerate_cameras


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BINDINGS_FILE = PROJECT_ROOT / "detector_config" / "camera_bindings.json"


def enumerate_devices() -> list:
    backend = cv2.CAP_DSHOW if sys.platform.startswith("win") else cv2.CAP_ANY
    return list(enumerate_cameras(backend))


def _normalized_path(value: str) -> str:
    return str(value or "").strip().casefold()


def load_bindings(path: Path = DEFAULT_BINDINGS_FILE) -> dict:
    if not path.is_file():
        return {"schema_version": 1, "bindings": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("bindings"), list):
        raise ValueError(f"Invalid camera binding file: {path}")
    return data


def save_binding(
    camera_index: int,
    camera_id: str,
    mqtt_camera_id: str = "",
    handoff_role: str = "off",
    peer_camera_id: str = "",
    path: Path = DEFAULT_BINDINGS_FILE,
    devices: Iterable | None = None,
) -> dict:
    available = list(devices) if devices is not None else enumerate_devices()
    device = next(
        (item for item in available if int(item.index) == int(camera_index)),
        None,
    )
    if device is None:
        raise LookupError(f"Camera index {camera_index} was not enumerated")
    device_path = str(getattr(device, "path", "") or "").strip()
    if not device_path:
        raise ValueError(
            f"Camera index {camera_index} has no stable USB path and cannot be bound"
        )
    logical_id = str(camera_id).strip()
    if not logical_id:
        raise ValueError("camera_id cannot be empty")
    if handoff_role not in {"off", "upstream", "downstream"}:
        raise ValueError(f"Invalid handoff role: {handoff_role}")
    binding = {
        "camera_id": logical_id,
        "mqtt_camera_id": str(mqtt_camera_id).strip() or logical_id,
        "device_path": device_path,
        "device_name": str(getattr(device, "name", "") or ""),
        "vid": getattr(device, "vid", None),
        "pid": getattr(device, "pid", None),
        "handoff_role": handoff_role,
        "peer_camera_id": str(peer_camera_id).strip(),
    }
    data = load_bindings(path)
    normalized = _normalized_path(device_path)
    data["bindings"] = [
        item
        for item in data["bindings"]
        if str(item.get("camera_id", "")) != logical_id
        and _normalized_path(item.get("device_path", "")) != normalized
    ]
    data["bindings"].append(binding)
    data["bindings"].sort(key=lambda item: str(item.get("camera_id", "")))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return binding


def binding_for(camera_id: str, path: Path = DEFAULT_BINDINGS_FILE) -> dict | None:
    logical_id = str(camera_id).strip()
    return next(
        (
            dict(item)
            for item in load_bindings(path)["bindings"]
            if str(item.get("camera_id", "")) == logical_id
        ),
        None,
    )


def resolve_camera_index(
    camera_id: str,
    path: Path = DEFAULT_BINDINGS_FILE,
    devices: Iterable | None = None,
) -> tuple[int, dict]:
    binding = binding_for(camera_id, path)
    if binding is None:
        raise LookupError(
            f"No USB binding exists for {camera_id!r}. Run camera-bind first."
        )
    expected_path = _normalized_path(binding.get("device_path", ""))
    available = list(devices) if devices is not None else enumerate_devices()
    device = next(
        (
            item
            for item in available
            if _normalized_path(getattr(item, "path", "")) == expected_path
        ),
        None,
    )
    if device is None:
        visible = [
            f"{getattr(item, 'index', '?')}:{getattr(item, 'name', '?')}"
            for item in available
        ]
        raise LookupError(
            f"Bound USB camera {camera_id!r} is not connected at its saved path. "
            f"Visible cameras: {', '.join(visible) or 'none'}"
        )
    return int(device.index), binding
