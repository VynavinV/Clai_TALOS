#!/usr/bin/env bash
set -euo pipefail

# Cross-platform installer for Clai TALOS
# Usage: curl -fsSL https://raw.githubusercontent.com/VynavinV/Clai_TALOS/master/scripts/install.sh | bash
#    or: bash install.sh [OPTIONS]
#
# Options:
#   --version, -v   Version to install (default: latest)
#   --prefix, -p    Install prefix (default: ~/.local for user, /usr/local for system)
#   --system        Install system-wide (requires sudo)
#   --uninstall     Remove Clai TALOS
#   --help, -h      Show help

REPO="VynavinV/Clai_TALOS"
BASE_URL="https://github.com/${REPO}/releases"

VERSION=""
PREFIX=""
SYSTEM=false
UNINSTALL=false

usage() {
  cat <<'EOF'
Usage: install.sh [OPTIONS]

Install Clai TALOS on Linux or macOS.

Options:
  -v, --version   Version to install (default: latest release)
  -p, --prefix    Install directory prefix (default: ~/.local)
  --system        Install system-wide to /usr/local (requires sudo)
  --uninstall     Remove Clai TALOS
  -h, --help      Show this help

Examples:
  curl -fsSL https://raw.githubusercontent.com/VynavinV/Clai_TALOS/master/scripts/install.sh | bash
  bash install.sh --version 0.1.0
  bash install.sh --system --prefix /usr/local
  bash install.sh --uninstall
EOF
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -v|--version)  VERSION="$2"; shift 2 ;;
    -p|--prefix)   PREFIX="$2"; shift 2 ;;
    --system)      SYSTEM=true; shift ;;
    --uninstall)   UNINSTALL=true; shift ;;
    -h|--help)     usage ;;
    *) echo "[fail] Unknown option: $1" >&2; exit 1 ;;
  esac
done

# --- Detect platform ---
detect_os() {
  local uname_s
  uname_s="$(uname -s)"
  case "$uname_s" in
    Linux*)  echo "linux" ;;
    Darwin*) echo "macos" ;;
    *)       echo "unknown"; return 1 ;;
  esac
}

detect_arch() {
  local uname_m
  uname_m="$(uname -m)"
  case "$uname_m" in
    x86_64|amd64)  echo "x64" ;;
    arm64|aarch64) echo "arm64" ;;
    *)             echo "$uname_m" ;;
  esac
}

OS="$(detect_os)"
ARCH="$(detect_arch)"

if [[ "$OS" == "unknown" ]]; then
  echo "[fail] Unsupported OS: $(uname -s)" >&2
  exit 1
fi

# --- Determine install prefix ---
if [[ -z "$PREFIX" ]]; then
  if $SYSTEM; then
    PREFIX="/usr/local"
  else
    PREFIX="$HOME/.local"
  fi
fi

INSTALL_DIR="$PREFIX/lib/clai-talos"
BIN_DIR="$PREFIX/bin"
APP_NAME="clai-talos"

# --- Uninstall ---
if $UNINSTALL; then
  echo "[info] Uninstalling Clai TALOS from $INSTALL_DIR ..."
  rm -f "$BIN_DIR/$APP_NAME"
  rm -rf "$INSTALL_DIR"

  # Remove systemd service if present
  if [[ -f "$PREFIX/lib/systemd/system/clai-talos.service" ]] || [[ -f "/etc/systemd/system/clai-talos.service" ]]; then
    echo "[info] Removing systemd service..."
    sudo systemctl stop clai-talos 2>/dev/null || true
    sudo systemctl disable clai-talos 2>/dev/null || true
    sudo rm -f /etc/systemd/system/clai-talos.service
    sudo rm -f "$PREFIX/lib/systemd/system/clai-talos.service"
    sudo systemctl daemon-reload
  fi

  # Remove launchd service if present (macOS)
  if [[ "$OS" == "macos" ]] && [[ -f "/Library/LaunchDaemons/com.claitalos.service.plist" ]]; then
    echo "[info] Removing launchd service..."
    sudo launchctl bootout system /Library/LaunchDaemons/com.claitalos.service.plist 2>/dev/null || true
    sudo rm -f /Library/LaunchDaemons/com.claitalos.service.plist
  fi

  echo "[ok] Clai TALOS uninstalled."
  exit 0
fi

# --- Get latest version if not specified ---
if [[ -z "$VERSION" ]]; then
  echo "[info] Fetching latest release..."
  VERSION="$(curl -fsSL "${BASE_URL}/latest" -w '%{url_effective}' -o /dev/null 2>/dev/null | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' | head -1 | sed 's/^v//')"
  if [[ -z "$VERSION" ]]; then
    echo "[fail] Could not determine latest version. Use --version to specify." >&2
    exit 1
  fi
  echo "[info] Latest version: $VERSION"
else
  VERSION="${VERSION#v}"
fi

echo "[info] Clai TALOS Installer"
echo "[info] OS:        $OS"
echo "[info] Arch:      $ARCH"
echo "[info] Version:   $VERSION"
echo "[info] Prefix:    $PREFIX"
echo "[info] Install dir: $INSTALL_DIR"
echo ""

# --- Determine asset name ---
ASSET=""
case "$OS" in
  linux)
    ASSET="clai-talos-${VERSION}-linux-${ARCH}.tar.gz"
    ;;
  macos)
    # Prefer .dmg for macOS, fall back to tar.gz
    if [[ "$ARCH" == "arm64" ]]; then
      ASSET="clai-talos_${VERSION}.dmg"
    else
      ASSET="clai-talos_${VERSION}.dmg"
    fi
    ;;
esac

DOWNLOAD_URL="${BASE_URL}/download/v${VERSION}/${ASSET}"
TARBALL="/tmp/clai-talos-install-${ASSET}"

echo "[info] Downloading: $ASSET"
echo "[info] From: $DOWNLOAD_URL"
echo ""

# --- Download ---
if command -v curl >/dev/null 2>&1; then
  curl -fSL --progress-bar "$DOWNLOAD_URL" -o "$TARBALL"
elif command -v wget >/dev/null 2>&1; then
  wget --show-progress -O "$TARBALL" "$DOWNLOAD_URL"
else
  echo "[fail] Neither curl nor wget found." >&2
  exit 1
fi

echo ""
echo "[ok] Downloaded $(du -h "$TARBALL" | cut -f1)"

# --- Extract and install ---
STAGING="/tmp/clai-talos-install-staging"
rm -rf "$STAGING"
mkdir -p "$STAGING"

case "$ASSET" in
  *.tar.gz)
    tar -xzf "$TARBALL" -C "$STAGING"
    ;;
  *.dmg)
    echo "[info] Mounting DMG..."
    MOUNT_POINT="$(hdiutil attach "$TARBALL" -nobrowse -plist 2>/dev/null | grep -oE '<string>/Volumes/[^<]+</string>' | head -1 | sed 's/<string>\/Volumes\///;s/<\/string>//')"
    if [[ -z "$MOUNT_POINT" ]]; then
      echo "[fail] Failed to mount DMG" >&2
      exit 1
    fi
    echo "[info] Mounted at: /Volumes/$MOUNT_POINT"
    # Copy .app from DMG
    mkdir -p "$STAGING/app"
    cp -R "/Volumes/$MOUNT_POINT/"*.app "$STAGING/app/" 2>/dev/null || true
    hdiutil detach "/Volumes/$MOUNT_POINT" 2>/dev/null || true
    ;;
esac

rm -f "$TARBALL"

# --- Install files ---
if $SYSTEM; then
  SUDO="sudo"
else
  SUDO=""
fi

$SUDO mkdir -p "$INSTALL_DIR" "$BIN_DIR"

# Handle different extracted structures
if [[ -d "$STAGING/clai-talos" ]]; then
  # Linux tarball: extracted as clai-talos/ directory
  $SUDO cp -r "$STAGING/clai-talos/"* "$INSTALL_DIR/"
  EXECUTABLE="$INSTALL_DIR/clai-talos"
elif [[ -d "$STAGING/app/Clai TALOS.app" ]] || [[ -d "$(ls -d "$STAGING/app/"*.app 2>/dev/null | head -1)" ]]; then
  # macOS .app
  APP_BUNDLE="$(ls -d "$STAGING/app/"*.app 2>/dev/null | head -1)"
  if [[ -n "$APP_BUNDLE" ]]; then
    $SUDO cp -R "$APP_BUNDLE" "$INSTALL_DIR/"
    EXECUTABLE="$INSTALL_DIR/$(basename "$APP_BUNDLE")/Contents/MacOS/Clai TALOS"
    # Fallback: the launcher script inside the .app
    if [[ ! -x "$EXECUTABLE" ]]; then
      EXECUTABLE="$(find "$INSTALL_DIR" -name 'start.sh' -path '*/MacOS/*' | head -1)"
    fi
  fi
elif [[ -f "$STAGING/clai-talos" ]]; then
  # Single binary
  $SUDO cp "$STAGING/clai-talos" "$INSTALL_DIR/"
  EXECUTABLE="$INSTALL_DIR/clai-talos"
else
  # Generic: copy everything
  $SUDO cp -r "$STAGING/"* "$INSTALL_DIR/"
  EXECUTABLE="$(find "$INSTALL_DIR" -maxdepth 2 -name 'clai-talos' -o -name 'start.sh' | head -1)"
fi

rm -rf "$STAGING"

if [[ -z "$EXECUTABLE" ]]; then
  echo "[fail] Could not find executable after extraction." >&2
  exit 1
fi

$SUDO chmod +x "$EXECUTABLE" 2>/dev/null || true

# --- Create launcher symlink or wrapper ---
LAUNCHER="$BIN_DIR/$APP_NAME"

if [[ "$OS" == "macos" ]] && [[ "$EXECUTABLE" == *.app* ]]; then
  # For .app bundles, create a wrapper script that opens the app
  cat > "/tmp/clai-talos-launcher" <<LAUNCH_EOF
#!/usr/bin/env bash
exec open "$EXECUTABLE" "\$@"
LAUNCH_EOF
  $SUDO install -m 755 "/tmp/clai-talos-launcher" "$LAUNCHER"
  rm -f "/tmp/clai-talos-launcher"
else
  # Symlink directly
  $SUDO ln -sf "$EXECUTABLE" "$LAUNCHER"
fi

echo ""
echo "[ok] Installed to $INSTALL_DIR"
echo "[ok] Launcher:    $LAUNCHER"

# --- Verify PATH ---
if ! echo "$PATH" | tr ':' '\n' | grep -qx "$BIN_DIR"; then
  echo ""
  echo "[warn] $BIN_DIR is not in your PATH."
  echo ""
  echo "Add it with:"
  echo ""
  SHELL_NAME="$(basename "${SHELL:-/bin/bash}")"
  case "$SHELL_NAME" in
    zsh)  echo "  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.zshrc && source ~/.zshrc" ;;
    bash) echo "  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.bashrc && source ~/.bashrc" ;;
    fish) echo "  echo 'set -gx PATH \"$BIN_DIR\" \$PATH' >> ~/.config/fish/config.fish && source ~/.config/fish/config.fish" ;;
    *)    echo "  export PATH=\"$BIN_DIR:\$PATH\"" ;;
  esac
  echo ""
fi

# --- Linux systemd service (optional) ---
if [[ "$OS" == "linux" ]] && $SYSTEM; then
  echo ""
  read -rp "[?] Install systemd service (auto-start on boot)? [y/N] " answer
  if [[ "$answer" =~ ^[Yy] ]]; then
    SERVICE_FILE="/etc/systemd/system/clai-talos.service"
    sudo tee "$SERVICE_FILE" > /dev/null <<SERVICE_EOF
[Unit]
Description=Clai TALOS AI Assistant
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$EXECUTABLE
WorkingDirectory=$INSTALL_DIR
Restart=on-failure
RestartSec=5
Environment=TALOS_DATA_DIR=%h/.local/share/clai_talos

[Install]
WantedBy=multi-user.target
SERVICE_EOF
    sudo systemctl daemon-reload
    sudo systemctl enable --now clai-talos
    echo "[ok] Systemd service installed and started."
    echo "[info] Check status: systemctl status clai-talos"
  fi
fi

echo ""
echo "========================================="
echo "  Clai TALOS v${VERSION} installed!"
echo "========================================="
echo ""
echo "Run it with:  $APP_NAME"
echo "Dashboard:    http://localhost:8080"
echo ""
echo "Uninstall:    bash <(curl -fsSL https://raw.githubusercontent.com/${REPO}/master/scripts/install.sh) --uninstall"
echo ""
