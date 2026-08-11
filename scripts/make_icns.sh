#!/usr/bin/env bash
# Build assets/pi-manager.icns on macOS from logo-1024.png
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/assets/logo-1024.png"
OUT_DIR="$ROOT/build/icon.iconset"
ICNS="$ROOT/assets/pi-manager.icns"

mkdir -p "$OUT_DIR"
sips -z 16 16     "$SRC" --out "$OUT_DIR/icon_16x16.png"
sips -z 32 32     "$SRC" --out "$OUT_DIR/icon_16x16@2x.png"
sips -z 32 32     "$SRC" --out "$OUT_DIR/icon_32x32.png"
sips -z 64 64     "$SRC" --out "$OUT_DIR/icon_32x32@2x.png"
sips -z 128 128   "$SRC" --out "$OUT_DIR/icon_128x128.png"
sips -z 256 256   "$SRC" --out "$OUT_DIR/icon_128x128@2x.png"
sips -z 256 256   "$SRC" --out "$OUT_DIR/icon_256x256.png"
sips -z 512 512   "$SRC" --out "$OUT_DIR/icon_256x256@2x.png"
sips -z 512 512   "$SRC" --out "$OUT_DIR/icon_512x512.png"
sips -z 1024 1024 "$SRC" --out "$OUT_DIR/icon_512x512@2x.png"
iconutil -c icns "$OUT_DIR" -o "$ICNS"
echo "wrote $ICNS"
