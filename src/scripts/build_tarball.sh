#!/usr/bin/env bash
set -euo pipefail

# Build a portable Linux tarball with source + pre-built venv
# Usage: ./src/scripts/build_tarball.sh [VERSION]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SRC_DIR/.." && pwd)"

PACKAGE_NAME="clai-talos"
VERSION="${1:-${TALOS_TARBALL_VERSION:-0.1.0}}"
OUT_DIR="${TARBALL_OUT_DIR:-$REPO_ROOT/dist/tarball}"
BUILD_DIR="$OUT_DIR/build"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "[fail] Missing required command: $1" >&2
    exit 1
  fi
}

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "[fail] Linux tarball builds are supported on Linux only." >&2
  exit 1
fi

require_cmd python3
require_cmd tar
require_cmd find

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64)  ARCH_TAG="x64" ;;
  aarch64) ARCH_TAG="arm64" ;;
  *)       ARCH_TAG="$ARCH" ;;
esac

if [[ ! "$VERSION" =~ ^[0-9A-Za-z][0-9A-Za-z.+~_-]*$ ]]; then
  echo "[fail] Invalid version string: $VERSION" >&2
  exit 1
fi

STAGING="$BUILD_DIR/${PACKAGE_NAME}-${VERSION}-linux-${ARCH_TAG}"

rm -rf "$BUILD_DIR"
mkdir -p "$STAGING"

# Copy source, excluding runtime artifacts
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
  . | tar -C "$STAGING" -xf -

# Fix line endings in shell scripts
find "$STAGING" -name "*.sh" -exec sed -i 's/\r$//' {} +
if [[ -f "$STAGING/start.sh" ]]; then
  chmod 0755 "$STAGING/start.sh"
fi

# Build venv with dependencies
echo "[info] Creating virtual environment..."
python3 -m venv "$STAGING/venv"
"$STAGING/venv/bin/pip" install --upgrade pip >/dev/null 2>&1
if [[ -f "$STAGING/src/requirements.txt" ]]; then
  echo "[info] Installing dependencies..."
  "$STAGING/venv/bin/pip" install -r "$STAGING/src/requirements.txt" >/dev/null 2>&1
fi

# Clean up venv caches
find "$STAGING/venv" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
rm -rf "$STAGING/venv/lib/python"*/site-packages/pip 2>/dev/null || true

# Create README for the tarball
cat > "$STAGING/README-INSTALL.txt" <<EOF
Clai TALOS v${VERSION} - Portable Linux Binary

Extract and run:
  tar -xzf ${PACKAGE_NAME}-${VERSION}-linux-${ARCH_TAG}.tar.gz
  cd ${PACKAGE_NAME}-${VERSION}-linux-${ARCH_TAG}
  ./start.sh              # Interactive setup
  ./start.sh --headless   # Headless mode

Or use the entry point directly:
  ./venv/bin/python src/talos_entry.py

System service (systemd):
  sudo cp clai-talos.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now clai-talos
EOF

# Create a systemd service file for convenience
cat > "$STAGING/clai-talos.service" <<EOF
[Unit]
Description=Clai TALOS Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$STAGING/src
Environment=TALOS_DATA_DIR=\$HOME/.clai-talos
ExecStart=$STAGING/venv/bin/python $STAGING/src/talos_entry.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

mkdir -p "$OUT_DIR"
OUTPUT_TARBALL="$OUT_DIR/${PACKAGE_NAME}-${VERSION}-linux-${ARCH_TAG}.tar.gz"

tar -C "$BUILD_DIR" -czf "$OUTPUT_TARBALL" "${PACKAGE_NAME}-${VERSION}-linux-${ARCH_TAG}"

echo "[ok] Built tarball: $OUTPUT_TARBALL"
echo "[next] Extract with: tar -xzf $OUTPUT_TARBALL"
echo "[next] Run with: cd ${PACKAGE_NAME}-${VERSION}-linux-${ARCH_TAG} && ./start.sh"
