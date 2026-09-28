#!/usr/bin/env bash
# Install the EpidBot app onto a MicroPythonOS device (ESP32) via mpremote.
# Usage: tools/install_device.sh [path-to-MicroPythonOS-checkout]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HERE/../com_kwarai_epidbot"
MPOS_DIR="${1:-${MPOS_DIR:-$HERE/../../MicroPythonOS}}"

if command -v mpremote >/dev/null 2>&1; then
  MP="mpremote"
elif [ -f "$MPOS_DIR/lvgl_micropython/lib/micropython/tools/mpremote/mpremote.py" ]; then
  MP="$MPOS_DIR/lvgl_micropython/lib/micropython/tools/mpremote/mpremote.py"
else
  echo "mpremote not found. Install it with: pip install mpremote"
  exit 1
fi

echo "Using mpremote: $MP"
"$MP" mkdir :/apps 2>/dev/null || true
"$MP" fs cp -r "$APP_DIR" :/apps/
echo "Copied com_kwarai_epidbot to device :/apps/"
