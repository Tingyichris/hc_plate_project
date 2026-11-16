"""Single public command-line entry point for the plate self-check project.

Camera options may be passed directly::

    python main.py --list-cameras
    python main.py --camera 1

Utility workflows use a subcommand; run ``python main.py --help`` for the list.
"""

from __future__ import annotations

import sys


HELP = """\
Plate distance self-check

Camera and color calibration:
  python main.py --list-cameras
  python main.py camera-bind --camera CAMERA_INDEX --camera-id LOGICAL_ID
  python main.py camera-bindings
  python main.py --camera-id LOGICAL_ID
  python main.py --camera CAMERA_INDEX [self-check options]
  python main.py self-check --camera CAMERA_INDEX [self-check options]
  python main.py color-calibration --camera CAMERA_INDEX

Other workflows:
  python main.py detect [--camera CAMERA_INDEX | --camera-id LOGICAL_ID]
  python main.py record (--camera CAMERA_INDEX | --camera-id LOGICAL_ID) --output FILE.mp4
  python main.py batch-preprocess INPUT_DIRECTORY OUTPUT_DIRECTORY [--roi FILE]
  python main.py evaluate OUTPUT_DIRECTORY --sequence COLORS [--removal EVENT]
  python main.py reanalyze-existing OUTPUT_DIRECTORY
  python main.py reanalyze-handoff OUTPUT_DIRECTORY

For color calibration, wait until the ROI is empty, press B, then insert
orange, green, and yellow plates in order.
Use "python main.py COMMAND --help" for command-specific parameters.
"""


def _self_check() -> None:
    from app.controllers.distance_self_check import main

    main()


def _batch_preprocess() -> None:
    from tools.batch_preprocess_images import main

    main()


def _evaluate() -> None:
    from app.controllers.evaluate_run import main

    main()


def _reanalyze_existing() -> None:
    from app.controllers.reanalyze_existing import main

    main()


def _reanalyze_handoff() -> None:
    from app.controllers.reanalyze_handoff import main

    main()


def _detect() -> None:
    from plate_detector.launcher import main

    main()


def _record() -> None:
    from tools.record_video import main

    main()


def _color_calibration() -> None:
    from tools.color_calibration import main

    main()


def _camera_bind() -> None:
    from tools.camera_bindings import bind_main

    bind_main()


def _camera_bindings() -> None:
    from tools.camera_bindings import list_main

    list_main()


COMMANDS: dict[str, tuple[str, list[str]]] = {
    "self-check": ("_self_check", []),
    "color-calibration": ("_color_calibration", []),
    "camera-bind": ("_camera_bind", []),
    "camera-bindings": ("_camera_bindings", []),
    "batch-preprocess": ("_batch_preprocess", []),
    "evaluate": ("_evaluate", []),
    "reanalyze-existing": ("_reanalyze_existing", []),
    "reanalyze-handoff": ("_reanalyze_handoff", []),
    "detect": ("_detect", []),
    "record": ("_record", []),
}


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in {"-h", "--help"}:
        print(HELP)
        return

    command = args[0]
    if command in COMMANDS:
        handler_name, forwarded_prefix = COMMANDS[command]
        sys.argv = [sys.argv[0], *forwarded_prefix, *args[1:]]
        globals()[handler_name]()
        return

    if command.startswith("-"):
        # Direct camera parameters default to the live self-check controller.
        _self_check()
        return

    available = ", ".join(COMMANDS)
    raise SystemExit(f"Unknown command: {command!r}. Available commands: {available}")


if __name__ == "__main__":
    main()
