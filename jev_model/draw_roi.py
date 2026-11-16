from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np


def read_image(image_path: Path) -> np.ndarray:
    data = np.fromfile(str(image_path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"Cannot read image: {image_path}")
    return image


def select_roi(image_path: Path, roi_path: Path) -> list[list[float]]:
    image = read_image(image_path)
    height, width = image.shape[:2]
    scale = min(1.0, 1200 / width, 800 / height)
    display_size = (round(width * scale), round(height * scale))
    base = cv2.resize(image, display_size, interpolation=cv2.INTER_AREA)
    points: list[tuple[int, int]] = []
    window = "Draw ROI - 4 points, Enter save, right click undo, R reset, Esc cancel"

    def on_mouse(event: int, x: int, y: int, _flags: int, _param: object) -> None:
        if event == cv2.EVENT_LBUTTONDOWN and len(points) < 4:
            original_x = max(0, min(width - 1, round(x / scale)))
            original_y = max(0, min(height - 1, round(y / scale)))
            points.append((original_x, original_y))
        elif event == cv2.EVENT_RBUTTONDOWN and points:
            points.pop()

    cv2.namedWindow(window, cv2.WINDOW_AUTOSIZE)
    cv2.setMouseCallback(window, on_mouse)

    while True:
        canvas = base.copy()
        shown = np.asarray(
            [(round(x * scale), round(y * scale)) for x, y in points],
            dtype=np.int32,
        )
        if len(shown) >= 3:
            overlay = canvas.copy()
            cv2.fillPoly(overlay, [shown], (80, 220, 80))
            cv2.addWeighted(overlay, 0.25, canvas, 0.75, 0, canvas)
        if len(shown) >= 2:
            cv2.polylines(canvas, [shown], len(shown) == 4, (0, 255, 255), 2)
        for index, (x, y) in enumerate(shown):
            cv2.circle(canvas, (int(x), int(y)), 6, (0, 80, 255), -1)
            cv2.putText(
                canvas,
                str(index + 1),
                (int(x) + 8, int(y) - 8),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (0, 80, 255),
                2,
                cv2.LINE_AA,
            )

        cv2.putText(
            canvas,
            f"Points: {len(points)}/4",
            (16, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.75,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        cv2.imshow(window, canvas)
        key = cv2.waitKey(20) & 0xFF
        if key in (13, 10) and len(points) == 4:
            break
        if key in (ord("r"), ord("R")):
            points.clear()
        if key == 27:
            cv2.destroyWindow(window)
            raise RuntimeError("ROI selection cancelled")

    cv2.destroyWindow(window)
    normalized = [[x / width, y / height] for x, y in points]
    roi_path.write_text(
        json.dumps(normalized, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"ROI saved: {roi_path.resolve()}")
    return normalized
