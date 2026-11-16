"""Tests for the single public command dispatcher."""

from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

import main


class MainTest(unittest.TestCase):
    def test_direct_camera_arguments_use_self_check(self):
        with patch.object(sys, "argv", ["main.py", "--camera", "2"]):
            with patch.object(main, "_self_check") as handler:
                main.main()
        handler.assert_called_once_with()

    def test_self_check_subcommand_is_removed_before_forwarding(self):
        with patch.object(
            sys, "argv", ["main.py", "self-check", "--camera", "1"]
        ):
            with patch.object(main, "_self_check") as handler:
                main.main()
                self.assertEqual(sys.argv, ["main.py", "--camera", "1"])
        handler.assert_called_once_with()

    def test_color_calibration_uses_self_check_controller(self):
        with patch.object(
            sys, "argv", ["main.py", "color-calibration", "--camera", "1"]
        ):
            with patch.object(main, "_color_calibration") as handler:
                main.main()
                self.assertEqual(sys.argv, ["main.py", "--camera", "1"])
        handler.assert_called_once_with()

    def test_record_subcommand_forwards_arguments_to_record_tool(self):
        with patch.object(
            sys,
            "argv",
            ["main.py", "record", "--camera", "2", "--output", "run.mp4"],
        ):
            with patch.object(main, "_record") as handler:
                main.main()
                self.assertEqual(
                    sys.argv,
                    ["main.py", "--camera", "2", "--output", "run.mp4"],
                )
        handler.assert_called_once_with()

    def test_camera_bind_dispatches_binding_tool(self):
        with patch.object(
            sys,
            "argv",
            ["main.py", "camera-bind", "--camera", "0", "--camera-id", "cam-2"],
        ):
            with patch.object(main, "_camera_bind") as handler:
                main.main()
                self.assertEqual(
                    sys.argv,
                    ["main.py", "--camera", "0", "--camera-id", "cam-2"],
                )
        handler.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
