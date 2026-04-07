#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SRC_DIR/.." && pwd)"

PACKAGE_NAME="clai-talos"
PKG_IDENTIFIER="com.claitalos.pkg"
SERVICE_LABEL="com.claitalos.service"
VERSION="${1:-${TALOS_PKG_VERSION:-0.1.0}}"
OUT_DIR="${PKG_OUT_DIR:-$REPO_ROOT/dist/pkg}"
BUILD_DIR="$OUT_DIR/build"
STAGING_ROOT="$BUILD_DIR/root"
PKG_SCRIPTS_DIR="$BUILD_DIR/scripts"
APP_DIR="$STAGING_ROOT/usr/local/lib/$PACKAGE_NAME"
BIN_DIR="$STAGING_ROOT/usr/local/bin"
LAUNCHD_DIR="$STAGING_ROOT/Library/LaunchDaemons"
STATE_DIR="$STAGING_ROOT/usr/local/var/$PACKAGE_NAME"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "[fail] Missing required command: $1" >&2
    exit 1
  fi
}

normalize_to_lf() {
  local file_path="$1"
  local tmp_path
  tmp_path="${file_path}.tmp"
  tr -d '\r' < "$file_path" > "$tmp_path"
  mv "$tmp_path" "$file_path"
}

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "[fail] macOS package builds are supported on macOS only." >&2
  echo "[hint] Run this script on a macOS machine: ./src/scripts/build_pkg.sh" >&2
  exit 1
fi

require_cmd pkgbuild
require_cmd python3
require_cmd tar
require_cmd tr
require_cmd find

if [[ ! "$VERSION" =~ ^[0-9A-Za-z][0-9A-Za-z.+~_-]*$ ]]; then
  echo "[fail] Invalid macOS package version string: $VERSION" >&2
  exit 1
fi

rm -rf "$BUILD_DIR"
mkdir -p "$APP_DIR" "$BIN_DIR" "$LAUNCHD_DIR" "$STATE_DIR" "$PKG_SCRIPTS_DIR"

# Copy repository into /usr/local/lib, excluding local/runtime artifacts.
tar -C "$SRC_DIR" -cf - \
  --exclude=.venv \
  --exclude=venv \
  --exclude=dist \
  --exclude=build \
  --exclude=logs \
  --exclude=projects \
  --exclude=.pytest_cache \
  --exclude='*.pyc' \
  --exclude=talos.db \
  --exclude=talos.db-wal \
  --exclude=talos.db-shm \
  --exclude=.env \
  --exclude=.credentials \
  --exclude=.google_oauth.json \
  --exclude=.security.log \
  --exclude=.setup_config \
  --exclude=.tools_config \
  --exclude=.himalaya \
  . | tar -C "$APP_DIR" -xf -

find "$APP_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} +
find "$APP_DIR" -type f -name "*.pyc" -delete

cat > "$BIN_DIR/clai" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || "${1:-}" == "help" ]]; then
  cat <<'EOHELP'
Usage:
  clai ./start.sh [args]

Examples:
  clai ./start.sh
  clai ./start.sh --headless
EOHELP
  exit 0
fi

if [[ -z "${1:-}" ]]; then
  echo "[fail] Missing script path. Example: clai ./start.sh --headless" >&2
  exit 1
fi

script_path="$1"
shift

if [[ ! -f "$script_path" ]]; then
  echo "[fail] Script not found: $script_path" >&2
  echo "[hint] For .pkg-only installs, use launchctl commands from README." >&2
  exit 1
fi

if grep -q $'\r' "$script_path" 2>/dev/null; then
  tmp_path="${script_path}.tmp"
  tr -d '\r' < "$script_path" > "$tmp_path"
  mv "$tmp_path" "$script_path"
fi

exec /usr/bin/env bash "$script_path" "$@"
EOF

cat > "$BIN_DIR/clai-talos" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/usr/local/lib/clai-talos"
export TALOS_DATA_DIR="${TALOS_DATA_DIR:-$HOME/.clai-talos}"
exec "$APP_DIR/venv/bin/python" "$APP_DIR/talos_entry.py" "$@"
EOF

cat > "$LAUNCHD_DIR/${SERVICE_LABEL}.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.claitalos.service</string>

  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/lib/clai-talos/venv/bin/python</string>
    <string>/usr/local/lib/clai-talos/talos_entry.py</string>
  </array>

  <key>WorkingDirectory</key>
  <string>/usr/local/lib/clai-talos</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>TALOS_DATA_DIR</key>
    <string>/usr/local/var/clai-talos</string>
  </dict>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>StandardOutPath</key>
  <string>/usr/local/var/clai-talos/logs/stdout.log</string>

  <key>StandardErrorPath</key>
  <string>/usr/local/var/clai-talos/logs/stderr.log</string>
</dict>
</plist>
EOF

cat > "$PKG_SCRIPTS_DIR/preinstall" <<'EOF'
#!/bin/bash
set -e

LABEL="com.claitalos.service"
PLIST="/Library/LaunchDaemons/${LABEL}.plist"

if command -v launchctl >/dev/null 2>&1; then
  launchctl disable "system/${LABEL}" >/dev/null 2>&1 || true
  launchctl bootout system "$PLIST" >/dev/null 2>&1 || true
fi

exit 0
EOF

cat > "$PKG_SCRIPTS_DIR/postinstall" <<'EOF'
#!/bin/bash
set -e

APP_DIR="/usr/local/lib/clai-talos"
DATA_DIR="/usr/local/var/clai-talos"
LOG_DIR="${DATA_DIR}/logs"
LABEL="com.claitalos.service"
PLIST="/Library/LaunchDaemons/${LABEL}.plist"

mkdir -p "$DATA_DIR" "$LOG_DIR"

if [ ! -d "$APP_DIR/venv" ]; then
  python3 -m venv "$APP_DIR/venv"
fi

"$APP_DIR/venv/bin/pip" install --upgrade pip >/dev/null 2>&1 || true
if [ -f "$APP_DIR/requirements.txt" ]; then
  "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" >/dev/null 2>&1 || true
fi

if command -v launchctl >/dev/null 2>&1; then
  launchctl bootstrap system "$PLIST" >/dev/null 2>&1 || true
  launchctl enable "system/${LABEL}" >/dev/null 2>&1 || true
  launchctl kickstart -k "system/${LABEL}" >/dev/null 2>&1 || true
fi

exit 0
EOF

chmod 0755 "$BIN_DIR/clai" "$BIN_DIR/clai-talos"
chmod 0644 "$LAUNCHD_DIR/${SERVICE_LABEL}.plist"
chmod 0755 "$PKG_SCRIPTS_DIR/preinstall" "$PKG_SCRIPTS_DIR/postinstall"

mkdir -p "$OUT_DIR"
OUTPUT_PKG="$OUT_DIR/${PACKAGE_NAME}_${VERSION}.pkg"

pkgbuild \
  --root "$STAGING_ROOT" \
  --scripts "$PKG_SCRIPTS_DIR" \
  --identifier "$PKG_IDENTIFIER" \
  --version "$VERSION" \
  --install-location "/" \
  --ownership recommended \
  "$OUTPUT_PKG"

echo "[ok] Built package: $OUTPUT_PKG"
echo "[next] Install with: sudo installer -pkg $OUTPUT_PKG -target /"
echo "[next] Start service: sudo launchctl kickstart -k system/$SERVICE_LABEL"
echo "[next] Check service: sudo launchctl print system/$SERVICE_LABEL"
echo "[next] Tail logs: tail -f /usr/local/var/$PACKAGE_NAME/logs/stderr.log"
