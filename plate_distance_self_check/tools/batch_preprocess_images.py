"""Batch-create model-ready images using detector preprocessing."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DETECTOR_ROOT = PROJECT_ROOT

from plate_detector import detector as det


IMAGE_SUFFIXES = {
    ".bmp", ".dib", ".jpeg", ".jpg", ".jpe", ".jp2",
    ".png", ".tif", ".tiff", ".webp",
}
DEFAULT_ROI_FILE = PROJECT_ROOT / "self_check_aoi.json"


def read_image(path: Path) -> np.ndarray | None:
    """Decode paths containing non-ASCII characters on Windows."""
    try:
        encoded = np.fromfile(path, dtype=np.uint8)
    except OSError:
        return None
    return cv2.imdecode(encoded, cv2.IMREAD_COLOR)


def write_png(path: Path, image: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError(f"Cannot encode image: {path}")
    encoded.tofile(path)


def load_roi(path: Path) -> np.ndarray:
    roi = np.asarray(json.loads(path.read_text(encoding="utf-8")), dtype=np.float32)
    if roi.shape != (4, 2) or np.any(roi < 0) or np.any(roi > 1):
        raise ValueError(f"ROI must contain four normalized [x, y] points: {path}")
    return roi


# Kept as the explicit public name used by replay tests and older callers.
load_normalized_roi = load_roi


def process_image(
    image: np.ndarray, normalized_roi: np.ndarray
) -> tuple[np.ndarray, np.ndarray, bool]:
    height, width = image.shape[:2]
    polygon = np.rint(
        normalized_roi * np.asarray([width, height], dtype=np.float32)
    ).astype(np.int32)
    polygon[:, 0] = np.clip(polygon[:, 0], 0, width - 1)
    polygon[:, 1] = np.clip(polygon[:, 1], 0, height - 1)
    original, masked, _x, _y, _inverse, warped = det.prepare_aoi_view(
        image, polygon, det.DEFAULT_WARP_ENABLED
    )
    processed = det.preprocess_aoi_for_inference(masked)
    return original, processed, bool(warped)


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_directory", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--roi", type=Path, default=DEFAULT_ROI_FILE)
    args = parser.parse_args()

    source = args.input_directory.resolve()
    output = args.output_directory.resolve()
    roi_file = args.roi.resolve()
    if not source.is_dir():
        raise NotADirectoryError(source)
    if source == output:
        raise ValueError("Input and output directories must be different.")
    normalized_roi = load_roi(roi_file)
    images = sorted(
        path for path in source.rglob("*")
        if path.is_file()
        and path.suffix.lower() in IMAGE_SUFFIXES
        and not is_within(path.resolve(), output)
    )

    successes = 0
    errors: list[str] = []
    for image_path in images:
        relative = image_path.relative_to(source)
        destination = output / ("__".join(relative.parts) + ".processed.png")
        try:
            image = read_image(image_path)
            if image is None:
                raise RuntimeError("OpenCV could not decode image")
            _aoi, processed, _warped = process_image(image, normalized_roi)
            write_png(destination, processed)
            successes += 1
        except Exception as error:  # Keep processing the remaining dataset.
            errors.append(f"{relative}: {error!r}")

    print(f"ROI: {roi_file}")
    print(f"Preprocess mode: {det.PREPROCESS_MODE}")
    print(f"Warp enabled: {det.DEFAULT_WARP_ENABLED}")
    print(f"Processed: {successes}/{len(images)}")
    print(f"Output: {output}")
    for error in errors:
        print(f"ERROR: {error}")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
