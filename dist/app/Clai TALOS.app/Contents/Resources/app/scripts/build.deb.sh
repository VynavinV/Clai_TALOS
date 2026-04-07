#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SRC_DIR/.." && pwd)"

PACKAGE_NAME="clai-talos"
VERSION="${1:-${TALOS_DEB_VERSION:-0.1.0}}"
ARCH="${DEB_ARCH:-}"
OUT_DIR="${DEB_OUT_DIR:-$REPO_ROOT/dist/deb}"
BUILD_DIR="${DEB_BUILD_DIR:-$OUT_DIR/build}"
TEMP_BUILD_DIR=""

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "[fail] Missing required command: $1" >&2
    exit 1
  fi
}

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "[fail] Debian package builds are supported on Linux only." >&2
  echo "[hint] On Windows, run this script via WSL: ./src/scripts/build_deb.ps1" >&2
  exit 1
fi

require_cmd dpkg-deb
require_cmd python3
require_cmd tar

if [[ -z "$ARCH" ]]; then
  ARCH="$(dpkg --print-architecture 2>/dev/null || echo "amd64")"
fi

if [[ ! "$VERSION" =~ ^[0-9A-Za-z][0-9A-Za-z.+:~_-]*$ ]]; then
  echo "[fail] Invalid Debian version string: $VERSION" >&2
  exit 1
fi

# /mnt/c and other Windows-backed mounts can ignore chmod semantics needed by dpkg-deb.
if [[ "$BUILD_DIR" == /mnt/* ]]; then
  BUILD_DIR="$(mktemp -d "/tmp/${PACKAGE_NAME}-deb-build.XXXXXX")"
  TEMP_BUILD_DIR="$BUILD_DIR"
fi

if [[ -n "$TEMP_BUILD_DIR" ]]; then
  trap 'rm -rf "$TEMP_BUILD_DIR"' EXIT
fi

PKG_DIR="$BUILD_DIR/${PACKAGE_NAME}_${VERSION}_${ARCH}"
DEBIAN_DIR="$PKG_DIR/DEBIAN"
APP_DIR="$PKG_DIR/opt/$PACKAGE_NAME"
BIN_DIR="$PKG_DIR/usr/bin"
SYSTEMD_DIR="$PKG_DIR/lib/systemd/system"
DEFAULTS_DIR="$PKG_DIR/etc/default"
VAR_DIR="$PKG_DIR/var/lib/$PACKAGE_NAME"

rm -rf "$PKG_DIR"
mkdir -p "$DEBIAN_DIR" "$APP_DIR" "$BIN_DIR" "$SYSTEMD_DIR" "$DEFAULTS_DIR" "$VAR_DIR"

# Copy repository into /opt, excluding local/runtime artifacts.
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
  --exclude=__pycache__ \
  --exclude='**/__pycache__' \
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

if [[ -f "$APP_DIR/start.sh" ]]; then
  sed -i 's/\r$//' "$APP_DIR/start.sh"
  chmod 0755 "$APP_DIR/start.sh"
fi

cat > "$DEBIAN_DIR/control" <<EOF
Package: $PACKAGE_NAME
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH
Maintainer: Clai TALOS Maintainers <maintainers@clai-talos.local>
Depends: bash, python3 (>= 3.10), python3-venv, python3-pip, ca-certificates
Description: Clai TALOS personal AI assistant
 Clai TALOS is a self-hosted AI assistant with Telegram and web dashboard interfaces,
 automation tools, and local persistence.
EOF

cat > "$DEBIAN_DIR/postinst" <<'EOF'
#!/bin/bash
set -e

APP_DIR="/opt/clai-talos"
DATA_DIR="/var/lib/clai-talos"
USER_NAME="clai-talos"
GROUP_NAME="clai-talos"
ENV_FILE="/etc/default/clai-talos"

if ! getent group "$GROUP_NAME" >/dev/null 2>&1; then
  groupadd --system "$GROUP_NAME" || true
fi

if ! id -u "$USER_NAME" >/dev/null 2>&1; then
  useradd \
    --system \
    --gid "$GROUP_NAME" \
    --home-dir "$DATA_DIR" \
    --create-home \
    --shell /usr/sbin/nologin \
    "$USER_NAME" || true
fi

mkdir -p "$DATA_DIR"
chown -R "$USER_NAME:$GROUP_NAME" "$DATA_DIR"

if [ ! -d "$APP_DIR/venv" ]; then
  python3 -m venv "$APP_DIR/venv"
fi

"$APP_DIR/venv/bin/pip" install --upgrade pip >/dev/null 2>&1 || true
if [ -f "$APP_DIR/src/requirements.txt" ]; then
  "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/src/requirements.txt" >/dev/null 2>&1 || true
fi

if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<'EODEFAULTS'
TALOS_DATA_DIR=/var/lib/clai-talos
WEB_PORT=8080
EODEFAULTS
fi

if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload || true
  systemctl enable clai-talos.service >/dev/null 2>&1 || true
fi

exit 0
EOF

cat > "$DEBIAN_DIR/prerm" <<'EOF'
#!/bin/bash
set -e

if command -v systemctl >/dev/null 2>&1; then
  systemctl stop clai-talos.service >/dev/null 2>&1 || true
  systemctl disable clai-talos.service >/dev/null 2>&1 || true
fi

exit 0
EOF

cat > "$DEBIAN_DIR/postrm" <<'EOF'
#!/bin/bash
set -e

if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload >/dev/null 2>&1 || true
fi

if [ "$1" = "purge" ]; then
  rm -f /etc/default/clai-talos
fi

exit 0
EOF

cat > "$DEBIAN_DIR/conffiles" <<'EOF'
/etc/default/clai-talos
EOF

cat > "$DEFAULTS_DIR/clai-talos" <<'EOF'
TALOS_DATA_DIR=/var/lib/clai-talos
WEB_PORT=8080
EOF

chmod 0755 "$DEBIAN_DIR"
chmod 0755 "$DEBIAN_DIR/postinst" "$DEBIAN_DIR/prerm" "$DEBIAN_DIR/postrm"
chmod 0644 "$DEFAULTS_DIR/clai-talos"

cat > "$SYSTEMD_DIR/clai-talos.service" <<'EOF'
[Unit]
Description=Clai TALOS Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=clai-talos
Group=clai-talos
WorkingDirectory=/opt/clai-talos/src
EnvironmentFile=-/etc/default/clai-talos
Environment=TALOS_DATA_DIR=/var/lib/clai-talos
ExecStart=/opt/clai-talos/venv/bin/python /opt/clai-talos/src/talos_entry.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > "$BIN_DIR/clai-talos" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/clai-talos"
export TALOS_DATA_DIR="${TALOS_DATA_DIR:-/var/lib/clai-talos}"
exec "$APP_DIR/venv/bin/python" "$APP_DIR/src/talos_entry.py" "$@"
EOF

cat > "$BIN_DIR/clai" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/clai-talos"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || "${1:-}" == "help" ]]; then
  cat <<'EOHELP'
Usage:
  clai [path-to-start.sh] [args]

Examples:
  clai ./start.sh --headless
EOHELP
  exit 0
fi

if [[ -n "${1:-}" && -f "$1" ]]; then
  script_path="$1"
  shift
  if grep -q $'\r' "$script_path" 2>/dev/null; then
    sed -i 's/\r$//' "$script_path" || true
  fi
  exec /usr/bin/env bash "$script_path" "$@"
fi

if [[ "${1:-}" == "./start.sh" || "${1:-}" == "start.sh" || "${1:-}" == "/opt/clai-talos/start.sh" ]]; then
  shift
fi

exec /usr/bin/env bash "$APP_DIR/start.sh" "$@"
EOF

chmod 0755 "$BIN_DIR/clai-talos"
chmod 0755 "$BIN_DIR/clai"

mkdir -p "$OUT_DIR"
OUTPUT_DEB="$OUT_DIR/${PACKAGE_NAME}_${VERSION}_${ARCH}.deb"

if dpkg-deb --help 2>/dev/null | grep -q -- "--root-owner-group"; then
  dpkg-deb --root-owner-group --build "$PKG_DIR" "$OUTPUT_DEB"
else
  dpkg-deb --build "$PKG_DIR" "$OUTPUT_DEB"
fi

echo "[ok] Built package: $OUTPUT_DEB"
echo "[next] Install with: sudo dpkg -i $OUTPUT_DEB"
echo "[next] Start service: sudo systemctl start clai-talos"
echo "[next] Check status: sudo systemctl status clai-talos"
