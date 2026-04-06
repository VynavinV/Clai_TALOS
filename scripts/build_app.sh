#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

APP_NAME="Clai TALOS.app"
BUNDLE_IDENTIFIER="com.claitalos.app"
VERSION="${1:-${TALOS_APP_VERSION:-0.1.0}}"
OUT_DIR="${APP_OUT_DIR:-$REPO_ROOT/dist/app}"
BUILD_DIR="$OUT_DIR/build"
APP_BUNDLE_BUILD_DIR="$BUILD_DIR/$APP_NAME"
CONTENTS_DIR="$APP_BUNDLE_BUILD_DIR/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
BUNDLED_APP_DIR="$RESOURCES_DIR/app"
FINAL_APP_PATH="$OUT_DIR/$APP_NAME"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "[fail] Missing required command: $1" >&2
    exit 1
  fi
}

normalize_to_lf() {
  local file_path="$1"
  local tmp_path="${file_path}.tmp"
  tr -d '\r' < "$file_path" > "$tmp_path"
  mv "$tmp_path" "$file_path"
}

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "[fail] macOS app builds are supported on macOS only." >&2
  echo "[hint] Run this script on a macOS machine: ./scripts/build_app.sh" >&2
  exit 1
fi

require_cmd python3
require_cmd tar
require_cmd tr
require_cmd find
require_cmd cp
require_cmd rm

if [[ ! "$VERSION" =~ ^[0-9A-Za-z][0-9A-Za-z.+~_-]*$ ]]; then
  echo "[fail] Invalid macOS app version string: $VERSION" >&2
  exit 1
fi

rm -rf "$BUILD_DIR"
mkdir -p "$MACOS_DIR" "$RESOURCES_DIR" "$BUNDLED_APP_DIR"

# Copy repository into app resources, excluding local/runtime artifacts.
tar -C "$REPO_ROOT" -cf - \
  --exclude=.git \
  --exclude=.github \
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
  . | tar -C "$BUNDLED_APP_DIR" -xf -

find "$BUNDLED_APP_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} +
find "$BUNDLED_APP_DIR" -type f -name "*.pyc" -delete

if [[ -f "$BUNDLED_APP_DIR/start.sh" ]]; then
  normalize_to_lf "$BUNDLED_APP_DIR/start.sh"
  chmod 0755 "$BUNDLED_APP_DIR/start.sh"
fi

cat > "$CONTENTS_DIR/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>
  <string>Clai TALOS</string>
  <key>CFBundleDisplayName</key>
  <string>Clai TALOS</string>
  <key>CFBundleIdentifier</key>
  <string>$BUNDLE_IDENTIFIER</string>
  <key>CFBundleVersion</key>
  <string>$VERSION</string>
  <key>CFBundleShortVersionString</key>
  <string>$VERSION</string>
  <key>CFBundleExecutable</key>
  <string>clai-talos-launcher</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>LSMinimumSystemVersion</key>
  <string>12.0</string>
</dict>
</plist>
EOF

echo "$VERSION" > "$RESOURCES_DIR/version.txt"

cat > "$MACOS_DIR/clai-talos-launcher" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESOURCES_DIR="$(cd "$SCRIPT_DIR/../Resources" && pwd)"
BUNDLED_APP_DIR="$RESOURCES_DIR/app"
BUNDLED_VERSION_FILE="$RESOURCES_DIR/version.txt"

RUNTIME_HOME="${HOME}/.clai-talos"
RUNTIME_APP_DIR="$RUNTIME_HOME/app"
RUNTIME_VENV_DIR="$RUNTIME_HOME/venv"
RUNTIME_LOG_DIR="$RUNTIME_HOME/logs"
RUNTIME_DATA_DIR="$RUNTIME_HOME/data"
RUNTIME_VERSION_FILE="$RUNTIME_HOME/version.txt"
RUNTIME_PID_FILE="$RUNTIME_HOME/talos.pid"

mkdir -p "$RUNTIME_HOME" "$RUNTIME_LOG_DIR" "$RUNTIME_DATA_DIR"

bundled_version="unknown"
if [[ -f "$BUNDLED_VERSION_FILE" ]]; then
  bundled_version="$(cat "$BUNDLED_VERSION_FILE")"
fi

needs_sync=0
if [[ ! -d "$RUNTIME_APP_DIR" ]]; then
  needs_sync=1
elif [[ ! -f "$RUNTIME_VERSION_FILE" ]]; then
  needs_sync=1
elif [[ "$(cat "$RUNTIME_VERSION_FILE")" != "$bundled_version" ]]; then
  needs_sync=1
fi

if [[ "$needs_sync" -eq 1 ]]; then
  rm -rf "$RUNTIME_APP_DIR"
  mkdir -p "$RUNTIME_APP_DIR"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete "$BUNDLED_APP_DIR/" "$RUNTIME_APP_DIR/"
  else
    cp -R "$BUNDLED_APP_DIR/." "$RUNTIME_APP_DIR/"
  fi
  printf "%s" "$bundled_version" > "$RUNTIME_VERSION_FILE"
fi

if [[ ! -x "$RUNTIME_VENV_DIR/bin/python" ]]; then
  python3 -m venv "$RUNTIME_VENV_DIR"
fi

"$RUNTIME_VENV_DIR/bin/pip" install --upgrade pip >/dev/null 2>&1 || true
if [[ -f "$RUNTIME_APP_DIR/requirements.txt" ]]; then
  "$RUNTIME_VENV_DIR/bin/pip" install -r "$RUNTIME_APP_DIR/requirements.txt" >/dev/null 2>&1 || true
fi
if [[ -f "$RUNTIME_APP_DIR/setup.py" ]]; then
  "$RUNTIME_VENV_DIR/bin/python" "$RUNTIME_APP_DIR/setup.py" >/dev/null 2>&1 || true
fi

web_port="8080"
if [[ -f "$RUNTIME_APP_DIR/.env" ]]; then
  env_port="$(grep '^WEB_PORT=' "$RUNTIME_APP_DIR/.env" 2>/dev/null | head -1 | cut -d'=' -f2-)"
  if [[ -n "${env_port:-}" ]]; then
    web_port="$env_port"
  fi
fi

if [[ -f "$RUNTIME_PID_FILE" ]]; then
  existing_pid="$(cat "$RUNTIME_PID_FILE" 2>/dev/null || true)"
  if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" >/dev/null 2>&1; then
    open "http://localhost:${web_port}" >/dev/null 2>&1 || true
    exit 0
  fi
fi

cd "$RUNTIME_APP_DIR"
export TALOS_DATA_DIR="${TALOS_DATA_DIR:-$RUNTIME_DATA_DIR}"

nohup "$RUNTIME_VENV_DIR/bin/python" "$RUNTIME_APP_DIR/telegram_bot.py" \
  > "$RUNTIME_LOG_DIR/stdout.log" \
  2> "$RUNTIME_LOG_DIR/stderr.log" &

echo $! > "$RUNTIME_PID_FILE"

sleep 2
open "http://localhost:${web_port}" >/dev/null 2>&1 || true
EOF

chmod 0755 "$MACOS_DIR/clai-talos-launcher"

mkdir -p "$OUT_DIR"
rm -rf "$FINAL_APP_PATH"
cp -R "$APP_BUNDLE_BUILD_DIR" "$FINAL_APP_PATH"

echo "[ok] Built app bundle: $FINAL_APP_PATH"
echo "[next] Open app: open '$FINAL_APP_PATH'"
echo "[next] Logs: tail -f ~/.clai-talos/logs/stderr.log"
