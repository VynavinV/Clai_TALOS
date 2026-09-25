#!/usr/bin/env bash
set -euo pipefail

# Unified cross-platform build orchestrator for Clai_TALOS
# Usage: ./scripts/build.sh [OPTIONS]
#
# Options:
#   --platform, -p   Target: linux, mac, windows, all (default: detect)
#   --version, -v    Version string (default: git tag or 0.1.0)
#   --type, -t       Build type: deb, tarball, app, dmg, pkg, exe (default: all for platform)
#   --output, -o     Output directory (default: dist/)
#   --clean          Clean build artifacts before building
#   --help, -h       Show help

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC_DIR="$REPO_ROOT/src"

PLATFORM=""
VERSION=""
BUILD_TYPE=""
OUTPUT_DIR="$REPO_ROOT/dist"
CLEAN=false

usage() {
  cat <<'EOF'
Usage: ./scripts/build.sh [OPTIONS]

Build Clai_TALOS distributable packages.

Options:
  -p, --platform   Target platform: linux, mac, windows, all (default: detect)
  -v, --version    Version string (default: from git or 0.1.0)
  -t, --type       Build type: deb, tarball, app, dmg, pkg, exe
                   (default: all types for platform)
  -o, --output     Output directory (default: dist/)
  --clean          Clean build artifacts before building
  -h, --help       Show this help

Examples:
  ./scripts/build.sh                          # Build for current platform
  ./scripts/build.sh -p linux -t deb          # Build .deb only
  ./scripts/build.sh -p linux -t tarball      # Build portable tarball
  ./scripts/build.sh -p all --clean           # Build everything possible
  ./scripts/build.sh -p mac -t dmg -v 1.2.0   # Build macOS .dmg v1.2.0
EOF
  exit 0
}

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    -p|--platform) PLATFORM="$2"; shift 2 ;;
    -v|--version)  VERSION="$2"; shift 2 ;;
    -t|--type)     BUILD_TYPE="$2"; shift 2 ;;
    -o|--output)   OUTPUT_DIR="$2"; shift 2 ;;
    --clean)       CLEAN=true; shift ;;
    -h|--help)     usage ;;
    *) echo "[fail] Unknown option: $1" >&2; exit 1 ;;
  esac
done

# Detect platform
detect_platform() {
  local uname_s
  uname_s="$(uname -s)"
  case "$uname_s" in
    Linux*)  echo "linux" ;;
    Darwin*) echo "mac" ;;
    MINGW*|MSYS*|CYGWIN*) echo "windows" ;;
    *) echo "unknown" ;;
  esac
}

if [[ -z "$PLATFORM" ]]; then
  PLATFORM="$(detect_platform)"
fi

# Determine version
get_version() {
  if [[ -n "$VERSION" ]]; then
    echo "$VERSION"
    return
  fi
  # Try git tag
  local tag
  tag="$(git describe --tags --abbrev=0 2>/dev/null || true)"
  if [[ -n "$tag" ]]; then
    echo "${tag#v}"
    return
  fi
  echo "0.1.0"
}

VERSION="$(get_version)"

echo "[info] Platform: $PLATFORM"
echo "[info] Version:  $VERSION"
echo "[info] Output:   $OUTPUT_DIR"
echo ""

# Clean if requested
if $CLEAN; then
  echo "[clean] Removing $OUTPUT_DIR ..."
  rm -rf "$OUTPUT_DIR"
fi

mkdir -p "$OUTPUT_DIR"

# Track what we built and what we can't build locally
built=()
skipped=()

build_linux_deb() {
  echo "=== Building Linux .deb ==="
  if ! bash "$SRC_DIR/scripts/build.deb.sh" "$VERSION"; then
    return 1
  fi
  mkdir -p "$OUTPUT_DIR/linux/deb"
  cp "$REPO_ROOT/dist/deb/"*.deb "$OUTPUT_DIR/linux/deb/" 2>/dev/null || true
  echo "[ok] .deb built"
}

build_linux_tarball() {
  echo "=== Building Linux tarball ==="
  if ! bash "$SRC_DIR/scripts/build_tarball.sh" "$VERSION"; then
    return 1
  fi
  mkdir -p "$OUTPUT_DIR/linux/tarball"
  cp "$REPO_ROOT/dist/tarball/"*.tar.gz "$OUTPUT_DIR/linux/tarball/" 2>/dev/null || true
  echo "[ok] tarball built"
}

build_linux_exe() {
  echo "=== Building Linux frozen binary ==="
  if ! command -v pyinstaller >/dev/null 2>&1; then
    echo "[warn] PyInstaller not found. Install with: pip install pyinstaller"
    return 1
  fi
  if [[ ! -f "$SRC_DIR/talos_linux.spec" ]]; then
    echo "[fail] Missing $SRC_DIR/talos_linux.spec"
    return 1
  fi
  (cd "$SRC_DIR" && pyinstaller --clean --noconfirm talos_linux.spec)
  mkdir -p "$OUTPUT_DIR/linux/exe"
  cp -r "$SRC_DIR/dist/clai-talos" "$OUTPUT_DIR/linux/exe/" 2>/dev/null || true
  echo "[ok] Linux binary built"
}

build_mac_app() {
  echo "=== Building macOS .app ==="
  if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "[skip] macOS .app requires building on macOS"
    return 1
  fi
  if ! bash "$SRC_DIR/scripts/build_app.sh" "$VERSION"; then
    return 1
  fi
  mkdir -p "$OUTPUT_DIR/mac/app"
  cp -r "$REPO_ROOT/dist/app/"* "$OUTPUT_DIR/mac/app/" 2>/dev/null || true
  echo "[ok] .app built"
}

build_mac_dmg() {
  echo "=== Building macOS .dmg ==="
  if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "[skip] macOS .dmg requires building on macOS"
    return 1
  fi
  if ! bash "$SRC_DIR/scripts/build_dmg.sh" "$VERSION"; then
    return 1
  fi
  mkdir -p "$OUTPUT_DIR/mac/dmg"
  cp "$REPO_ROOT/dist/dmg/"*.dmg "$OUTPUT_DIR/mac/dmg/" 2>/dev/null || true
  echo "[ok] .dmg built"
}

build_mac_pkg() {
  echo "=== Building macOS .pkg ==="
  if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "[skip] macOS .pkg requires building on macOS"
    return 1
  fi
  if ! bash "$SRC_DIR/scripts/build_pkg.sh" "$VERSION"; then
    return 1
  fi
  mkdir -p "$OUTPUT_DIR/mac/pkg"
  cp "$REPO_ROOT/dist/pkg/"*.pkg "$OUTPUT_DIR/mac/pkg/" 2>/dev/null || true
  echo "[ok] .pkg built"
}

build_windows_exe() {
  echo "=== Building Windows .exe ==="
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) ;;
    *)
      echo "[skip] Windows .exe requires building on Windows (or use PyInstaller cross-compile)"
      return 1
      ;;
  esac
  if ! command -v pyinstaller >/dev/null 2>&1; then
    echo "[warn] PyInstaller not found. Install with: pip install pyinstaller"
    return 1
  fi
  (cd "$SRC_DIR" && pyinstaller --clean --noconfirm talos_exe.spec)
  mkdir -p "$OUTPUT_DIR/windows/exe"
  cp -r "$SRC_DIR/dist/ClaiTALOS" "$OUTPUT_DIR/windows/exe/" 2>/dev/null || true
  echo "[ok] Windows .exe built"
}

# Build based on platform and type
do_build() {
  local plat="$1"
  local types=()

  if [[ -n "$BUILD_TYPE" ]]; then
    types=("$BUILD_TYPE")
  else
    case "$plat" in
      linux)   types=(deb tarball exe) ;;
      mac)     types=(app dmg pkg) ;;
      windows) types=(exe) ;;
    esac
  fi

  for t in "${types[@]}"; do
    case "$t" in
      deb)     build_linux_deb && built+=("linux/deb") || skipped+=("linux/deb") ;;
      tarball) build_linux_tarball && built+=("linux/tarball") || skipped+=("linux/tarball") ;;
      exe)
        case "$plat" in
          linux)   build_linux_exe && built+=("linux/exe") || skipped+=("linux/exe") ;;
          windows) build_windows_exe && built+=("windows/exe") || skipped+=("windows/exe") ;;
          *)       skipped+=("$plat/exe") ;;
        esac
        ;;
      app)  build_mac_app && built+=("mac/app") || skipped+=("mac/app") ;;
      dmg)  build_mac_dmg && built+=("mac/dmg") || skipped+=("mac/dmg") ;;
      pkg)  build_mac_pkg && built+=("mac/pkg") || skipped+=("mac/pkg") ;;
      *)
        echo "[fail] Unknown build type: $t" >&2
        echo "[hint] Valid types: deb, tarball, app, dmg, pkg, exe" >&2
        exit 1
        ;;
    esac
  done
}

case "$PLATFORM" in
  linux|mac|windows)
    do_build "$PLATFORM"
    ;;
  all)
    echo "[info] Building for all platforms (skipping what requires other OSes)..."
    echo ""
    do_build "linux"
    echo ""
    do_build "mac"
    echo ""
    do_build "windows"
    ;;
  *)
    echo "[fail] Unknown platform: $PLATFORM" >&2
    echo "[hint] Valid platforms: linux, mac, windows, all" >&2
    exit 1
    ;;
esac

echo ""
echo "========================================="
echo "  Build Summary"
echo "========================================="
if [[ ${#built[@]} -gt 0 ]]; then
  echo "[built]   ${built[*]}"
fi
if [[ ${#skipped[@]} -gt 0 ]]; then
  echo "[skipped] ${skipped[*]}"
  echo ""
  echo "To build skipped items, run on the appropriate platform:"
  echo "  macOS (.app/.dmg/.pkg):  ./scripts/build.sh -p mac"
  echo "  Windows (.exe):          ./scripts/build.sh -p windows"
fi
echo ""
echo "Output directory: $OUTPUT_DIR"

# Exit with error if a specific type was requested but nothing was built
if [[ -n "$BUILD_TYPE" && ${#built[@]} -eq 0 ]]; then
  echo ""
  echo "[fail] Requested build type '$BUILD_TYPE' could not be built." >&2
  exit 1
fi
