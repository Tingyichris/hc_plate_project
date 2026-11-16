# Plate detection project entry points

`plate_distance_self_check` is the unified operator-facing project. Its
`plate_detector` package contains the production detector, preprocessing,
tracker, recorder, inference client, and MQTT integration used by both live
modes.

## Live self-check and handoff

Use `main.py` for distance-stability capture, continuous MQTT
handoff, and its evidence outputs. For example:

```powershell
python .\main.py --camera 2 --handoff-role upstream `
  --camera-id cam-2 --peer-camera-id cam-3 --mqtt-host 127.0.0.1 `
  --mqtt-port 1883 --continuous-handoff --autosave-seconds 5 --matching-mode fifo
```

## Production detector

```powershell
python .\main.py detect
```

This uses the local `plate_detector` package. Runtime camera/model settings are
in `plate_detector/constants.py`; `detector_config` contains the AOI assets.
The detector can save an annotated inference video when `DETECTION_OUTPUT_VIDEO`
is configured. Its recorder continues to write detection, lifecycle, crossing,
batch, and summary evidence under `RECORDING_ROOT`.

## Raw camera recording

```powershell
python .\main.py record --camera 2 `
  --output .\outputs\videos\cam-2-test.mp4 --width 1280 --height 720 --fps 30
```

Press `Q` in the camera window to stop. The output directory is created if
needed. This records raw camera frames; use the production detector's video
output setting to record annotated inference frames.

## Batch image preprocessing

```powershell
python .\main.py batch-preprocess `
  "D:\path\to\input-images" "D:\path\to\processed-images"
```

The tool reads the normalized four-point ROI from `self_check_aoi.json` by
default. An alternate ROI can be passed with `--roi path\to\roi.json`. Input
and output directories must differ. Relative source paths are encoded in output
filenames to avoid collisions.

## Workspace dependency map

```text
hc_detext_map                 backend, fusion and dashboard
plate_distance_self_check     operator entry points, evaluation and run evidence
plate_detector/                 integrated detector implementation
detector_config/                integrated camera and model config
plate_detect_cam2              migration reference only
plate_detect                   migration reference only
```

Runtime imports now use the package and detector configuration inside this
project. The old detector trees and batch-preprocessing script are retained as
migration references; new runs should use the integrated entry point above.
