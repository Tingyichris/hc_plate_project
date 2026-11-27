#!/usr/bin/env bash
# Run on the Raspberry Pi: updates /home/hc/plate_detect to the device/ + validation/ layout.
set -euo pipefail
cd /home/hc
STAMP=$(date +%Y%m%d_%H%M%S)
cp -a plate_detect "plate_detect_backup_$STAMP"
echo "Backup: /home/hc/plate_detect_backup_$STAMP"
cd plate_detect

# Keep this Pi's own config (pi_id, AOI, bindings); move it into device/.
mkdir -p device
if [ -d detector_config ] && [ ! -d device/detector_config ]; then
  mv detector_config device/detector_config
fi

# Remove the old root-level code layout (the backup above keeps a copy).
rm -rf main.py plate_detector app tools tests reports baselines deploy requirements.txt

tar -xf /home/hc/plate_detect_update.tar
find . -name __pycache__ -type d -prune -exec rm -rf {} +

# Reset bindings so only real cameras get cam-N IDs.
python3 - <<'EOF'
import json, pathlib
p = pathlib.Path("device/detector_config/device_config.json")
d = json.loads(p.read_text()) if p.is_file() else {"schema_version": 4}
d["cameras"] = []
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(d, indent=2, ensure_ascii=False))
EOF

PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python
"$PY" device/main.py camera-bindings
