from __future__ import annotations

import argparse
import csv
import json
import random
from pathlib import Path

from PIL import Image, ImageDraw

from draw_roi import select_roi


DEFAULT_IMAGE_DIR = Path(r"D:\訓練集\訓練\camera1_20260916_161_frames")
DEFAULT_ROI_FILE = Path(__file__).with_name("roi.json")
SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}


def find_images(folder: Path) -> list[Path]:
    if not folder.is_dir():
        raise FileNotFoundError(f"Image folder does not exist: {folder}")
    return sorted(
        path
        for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )


def load_roi_polygon(roi_file: Path, width: int, height: int) -> list[tuple[int, int]]:
    if not roi_file.is_file():
        raise FileNotFoundError(f"ROI file does not exist: {roi_file}")

    points = json.loads(roi_file.read_text(encoding="utf-8"))
    if not isinstance(points, list) or len(points) < 3:
        raise ValueError("ROI must contain at least three normalized points")

    polygon: list[tuple[int, int]] = []
    for point in points:
        if not isinstance(point, list) or len(point) < 2:
            raise ValueError(f"Invalid ROI point: {point!r}")
        x = max(0, min(width - 1, round(float(point[0]) * width)))
        y = max(0, min(height - 1, round(float(point[1]) * height)))
        polygon.append((x, y))
    return polygon


def export_pixels(
    image_path: Path,
    output_path: Path,
    roi_file: Path,
    preview_count: int | None,
) -> None:
    with Image.open(image_path) as source:
        image = source.convert("RGB")
        width, height = image.size
        pixels = image.load()
        polygon = load_roi_polygon(roi_file, width, height)
        roi_mask = Image.new("1", (width, height), 0)
        ImageDraw.Draw(roi_mask).polygon(polygon, fill=1)
        mask_pixels = roi_mask.load()

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(["x", "y", "r", "g", "b"])

            previewed = 0
            exported = 0
            for y in range(height):
                for x in range(width):
                    if not mask_pixels[x, y]:
                        continue
                    red, green, blue = pixels[x, y]
                    writer.writerow([x, y, red, green, blue])
                    exported += 1
                    if preview_count is None or previewed < preview_count:
                        print(f"({x}, {y}) -> RGB({red}, {green}, {blue})")
                        previewed += 1

    print(f"Image: {image_path}")
    print(f"Size: {width} x {height}")
    print(f"ROI file: {roi_file}")
    print(f"ROI polygon: {polygon}")
    print(f"ROI pixel count: {exported}")
    print(f"CSV: {output_path.resolve()}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Select an image and export ROI pixels as x, y, r, g, b."
    )
    parser.add_argument(
        "--roi",
        type=Path,
        default=DEFAULT_ROI_FILE,
        help=f"Normalized polygon JSON file (default: {DEFAULT_ROI_FILE})",
    )
    parser.add_argument(
        "--draw-roi",
        action="store_true",
        help="Open the selected image and draw a new four-point ROI before exporting.",
    )
    parser.add_argument(
        "--image-dir",
        type=Path,
        default=DEFAULT_IMAGE_DIR,
        help=f"Folder to choose an image from (default: {DEFAULT_IMAGE_DIR})",
    )
    parser.add_argument(
        "--image",
        type=Path,
        help="Use one specific image instead of choosing randomly.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).with_name("pixels.csv"),
        help="Destination CSV path.",
    )
    parser.add_argument(
        "--preview",
        type=int,
        default=20,
        help="Number of pixel points to print to the terminal; use -1 for all.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.preview < -1:
        raise ValueError("--preview must be -1 or greater")

    if args.image is not None:
        image_path = args.image
        if not image_path.is_file():
            raise FileNotFoundError(f"Image does not exist: {image_path}")
    else:
        images = find_images(args.image_dir)
        if not images:
            raise FileNotFoundError(f"No supported images found in: {args.image_dir}")
        image_path = random.choice(images)

    if args.draw_roi:
        select_roi(image_path, args.roi)
    elif not args.roi.is_file():
        raise FileNotFoundError(
            f"ROI file does not exist: {args.roi}. Run again with --draw-roi."
        )

    preview_count = None if args.preview == -1 else args.preview
    export_pixels(image_path, args.output, args.roi, preview_count)


if __name__ == "__main__":
    main()
