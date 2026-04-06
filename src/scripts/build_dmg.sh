#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

APP_NAME="Clai TALOS.app"
VERSION="${1:-${TALOS_DMG_VERSION:-0.1.0}}"
APP_OUT_DIR="${APP_OUT_DIR:-$REPO_ROOT/dist/app}"
APP_PATH="$APP_OUT_DIR/$APP_NAME"
OUT_DIR="${DMG_OUT_DIR:-$REPO_ROOT/dist/dmg}"
BUILD_DIR="$OUT_DIR/build"
STAGING_DIR="$BUILD_DIR/staging"
VOLUME_NAME="Clai TALOS ${VERSION}"
OUTPUT_DMG="$OUT_DIR/clai-talos_${VERSION}.dmg"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "[fail] Missing required command: $1" >&2
    exit 1
  fi
}

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "[fail] macOS DMG builds are supported on macOS only." >&2
  echo "[hint] Run this script on a macOS machine: ./scripts/build_dmg.sh" >&2
  exit 1
fi

require_cmd hdiutil
require_cmd cp
require_cmd ln
require_cmd rm
require_cmd mkdir

if [[ ! "$VERSION" =~ ^[0-9A-Za-z][0-9A-Za-z.+~_-]*$ ]]; then
  echo "[fail] Invalid macOS dmg version string: $VERSION" >&2
  exit 1
fi

if [[ ! -d "$APP_PATH" ]]; then
  echo "[info] App bundle not found. Building app first..."
  bash "$SCRIPT_DIR/build_app.sh" "$VERSION"
fi

rm -rf "$STAGING_DIR"
mkdir -p "$STAGING_DIR"

cp -R "$APP_PATH" "$STAGING_DIR/$APP_NAME"
ln -s /Applications "$STAGING_DIR/Applications"

cat > "$STAGING_DIR/README.txt" <<EOF
Clai TALOS macOS Installer

1. Drag \"$APP_NAME\" into the Applications alias.
2. Open /Applications/$APP_NAME.
3. The app initializes TALOS under ~/.clai-talos and opens the dashboard.

Logs:
  ~/.clai-talos/logs/stdout.log
  ~/.clai-talos/logs/stderr.log
EOF

mkdir -p "$OUT_DIR"
rm -f "$OUTPUT_DMG"

hdiutil create \
  -volname "$VOLUME_NAME" \
  -srcfolder "$STAGING_DIR" \
  -ov \
  -format UDZO \
  "$OUTPUT_DMG"

echo "[ok] Built dmg: $OUTPUT_DMG"
echo "[next] Mount dmg: hdiutil attach '$OUTPUT_DMG'"
echo "[next] Open app: open '/Volumes/$VOLUME_NAME/$APP_NAME'"
