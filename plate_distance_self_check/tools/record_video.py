"""Record an unprocessed camera stream to an MP4 file as an offline tool."""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2

from plate_detector.camera_registry import resolve_camera_index


def record(
    camera: int,
    output: Path,
    width: int = 1280,
    height: int = 720,
    fps: float = 30.0,
) -> None:
    capture = cv2.VideoCapture(camera, cv2.CAP_DSHOW)
    if not capture.isOpened():
        raise RuntimeError(f"Could not open camera {camera}")
    if width:
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    if height:
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    if fps:
        capture.set(cv2.CAP_PROP_FPS, fps)

    frame_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    effective_fps = capture.get(cv2.CAP_PROP_FPS) or fps or 30.0
    destination = output.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(destination),
        cv2.VideoWriter_fourcc(*"mp4v"),
        effective_fps,
        (frame_width, frame_height),
    )
    if not writer.isOpened():
        capture.release()
        raise RuntimeError(f"Could not create video file: {destination}")

    print(
        f"Recording camera {camera} at {frame_width}x{frame_height}, "
        f"{effective_fps:.1f} FPS"
    )
    print(f"Output: {destination} (press Q to stop)")
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                print("Camera frame read failed; stopping recording.")
                break
            writer.write(frame)
            cv2.imshow("Plate camera recording", frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
    finally:
        writer.release()
        capture.release()
        cv2.destroyAllWindows()
    print(f"Recording saved: {destination}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--camera", type=int, help="explicit current camera index")
    selection.add_argument("--camera-id", default="", help="saved logical camera ID")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=float, default=30.0)
    args = parser.parse_args()
    camera_index = args.camera
    if args.camera_id:
        camera_index, _binding = resolve_camera_index(args.camera_id)
        print(
            f"Resolved {args.camera_id} to current camera index {camera_index} "
            "via saved USB path."
        )
    record(camera_index, args.output, args.width, args.height, args.fps)


if __name__ == "__main__":
    main()
