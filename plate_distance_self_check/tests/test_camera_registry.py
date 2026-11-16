"""Tests for stable logical camera IDs resolved from USB device paths."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from plate_detector.camera_registry import (
    binding_for,
    resolve_camera_index,
    save_binding,
)


def camera(index: int, path: str, name: str = "USB Camera"):
    return SimpleNamespace(index=index, path=path, name=name, vid=123, pid=456)


class CameraRegistryTest(unittest.TestCase):
    def test_binding_follows_usb_path_when_index_changes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "camera_bindings.json"
            save_binding(
                camera_index=0,
                camera_id="cam-2",
                mqtt_camera_id="mqtt-cam-2",
                handoff_role="upstream",
                peer_camera_id="mqtt-cam-3",
                path=path,
                devices=[camera(0, r"\\?\usb#port-a")],
            )
            index, binding = resolve_camera_index(
                "cam-2",
                path=path,
                devices=[camera(3, r"\\?\USB#PORT-A")],
            )
            self.assertEqual(index, 3)
            self.assertEqual(binding["mqtt_camera_id"], "mqtt-cam-2")
            self.assertEqual(binding["handoff_role"], "upstream")

    def test_rebinding_camera_id_replaces_old_usb_path(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "camera_bindings.json"
            save_binding(0, "cam-2", path=path, devices=[camera(0, "port-a")])
            save_binding(1, "cam-2", path=path, devices=[camera(1, "port-b")])
            self.assertEqual(binding_for("cam-2", path)["device_path"], "port-b")
            with self.assertRaises(LookupError):
                resolve_camera_index(
                    "cam-2", path=path, devices=[camera(0, "port-a")]
                )

    def test_camera_without_device_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "camera_bindings.json"
            with self.assertRaises(ValueError):
                save_binding(1, "virtual", path=path, devices=[camera(1, "")])


if __name__ == "__main__":
    unittest.main()
