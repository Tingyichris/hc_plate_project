import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[2] / "plate_detect" / "legacy"),
)
from tools import batch_preprocess_images as batch


class BatchPreprocessImagesTest(unittest.TestCase):
    def test_recursive_batch_matches_self_check_preprocessing(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_root = root / "輸入"
            output_root = root / "輸出"
            nested = input_root / "子資料夾"
            nested.mkdir(parents=True)

            image = np.zeros((80, 120, 3), dtype=np.uint8)
            image[10:75, 20:100] = (40, 120, 220)
            source = nested / "盤子.jpg"
            ok, encoded = cv2.imencode(".jpg", image)
            self.assertTrue(ok)
            encoded.tofile(source)

            roi_file = root / "roi.json"
            roi_file.write_text(
                json.dumps([[0.15, 0.1], [0.15, 0.95], [0.85, 0.95], [0.85, 0.1]]),
                encoding="utf-8",
            )

            with patch.object(batch, "DEFAULT_ROI_FILE", roi_file):
                with patch.object(
                    sys,
                    "argv",
                    [
                        "tools.batch_preprocess_images",
                        str(input_root),
                        str(output_root),
                    ],
                ):
                    batch.main()

            processed_path = output_root / "子資料夾__盤子.jpg.processed.png"
            self.assertTrue(processed_path.exists())

            decoded = batch.read_image(source)
            normalized = batch.load_normalized_roi(roi_file)
            _expected_roi, expected_processed, _warped = batch.process_image(
                decoded, normalized
            )
            actual_processed = batch.read_image(processed_path)
            np.testing.assert_array_equal(actual_processed, expected_processed)
            self.assertEqual(
                [path.name for path in output_root.iterdir()],
                ["子資料夾__盤子.jpg.processed.png"],
            )


if __name__ == "__main__":
    unittest.main()
