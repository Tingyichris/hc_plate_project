#!/usr/bin/env bash
# Run on the Raspberry Pi: find a Python that has OpenCV, then rebind cameras.
cd /home/hc/plate_detect
CANDIDATES=(.venv/bin/python /home/hc/.venv/bin/python /home/hc/venv/bin/python)
for p in /home/hc/*/.venv/bin/python /home/hc/*/venv/bin/python /home/hc/plate_detect_backup_*/.venv/bin/python; do
  CANDIDATES+=("$p")
done
CANDIDATES+=(python3)
PY=""
for p in "${CANDIDATES[@]}"; do
  if [ -x "$p" ] || command -v "$p" >/dev/null 2>&1; then
    if "$p" -c "import cv2, cv2_enumerate_cameras" >/dev/null 2>&1; then PY="$p"; break; fi
  fi
done
echo "--- systemd service:"
systemctl --user cat plate-detect 2>/dev/null | grep -E "ExecStart|WorkingDirectory" || \
  systemctl cat plate-detect 2>/dev/null | grep -E "ExecStart|WorkingDirectory" || echo "(no plate-detect service)"
if [ -z "$PY" ]; then
  echo "--- No Python with cv2 + cv2_enumerate_cameras found. Checked: ${CANDIDATES[*]}"
  exit 1
fi
echo "--- Using Python: $PY"
"$PY" device/main.py camera-bindings
