"""Launch one-off detector color calibration through the self-check workflow.

The live classifier remains in ``plate_detector.detector`` because production
inference uses it. This tool only exposes the evidence-collection workflow.
"""

from __future__ import annotations


def main() -> None:
    from app.controllers.distance_self_check import main as run_self_check

    run_self_check()


if __name__ == "__main__":
    main()
