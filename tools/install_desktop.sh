#!/usr/bin/env bash
# Install the EpidBot app into a MicroPythonOS checkout for desktop testing.
# Usage: tools/install_desktop.sh [path-to-MicroPythonOS-checkout]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HERE/../com_kwarai_epidbot"
MPOS_DIR="${1:-${MPOS_DIR:-$HERE/../../MicroPythonOS}}"
DEST="$MPOS_DIR/internal_filesystem/apps"

if [ ! -d "$DEST" ]; then
  echo "MicroPythonOS checkout with '$DEST' not found (got: $MPOS_DIR)"
  echo "Clone it: git clone https://github.com/MicroPythonOS/MicroPythonOS.git"
  exit 1
fi

rm -rf "$DEST/com_kwarai_epidbot"
cp -r "$APP_DIR" "$DEST/"
echo "Installed com_kwarai_epidbot -> $DEST"
echo "Launch MicroPythonOS and start 'EpidBot' from the launcher, or run in the REPL:"
echo "  from mpos import AppManager; AppManager.start_app('com_kwarai_epidbot')"
