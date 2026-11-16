"""Regression checks for MVC module compatibility and project-root paths."""

from __future__ import annotations

import unittest
from pathlib import Path

from app.controllers import distance_self_check as self_check_controller
from tools import batch_preprocess_images


class MvcStructureTest(unittest.TestCase):
    def test_controllers_keep_repository_root_for_data_paths(self):
        expected = Path(__file__).resolve().parents[1]
        self.assertEqual(batch_preprocess_images.PROJECT_ROOT, expected)
        self.assertEqual(self_check_controller.PROJECT_ROOT, expected)
        self.assertEqual(
            batch_preprocess_images.DEFAULT_ROI_FILE,
            expected / "self_check_aoi.json",
        )


if __name__ == "__main__":
    unittest.main()
