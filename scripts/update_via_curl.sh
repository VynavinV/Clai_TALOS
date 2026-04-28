#!/usr/bin/env bash

set -euo pipefail

CHANNEL="stable"
TARGET=""
REPO="vynavinv/clai-talos"
BRANCH="master"

usage() {
  cat <<'EOF'
Usage: update_via_curl.sh [options]

Options:
  --target PATH         Path to the Clai_TALOS install to update (default: current directory)
  --channel CHANNEL     stable or prerelease (default: stable)
  --repo OWNER/NAME     GitHub repo for releases (default: vynavinv/clai-talos)
  --branch NAME         Branch to download updater script from (default: master)
  -h, --help            Show this help

Examples:
  update_via_curl.sh --target /opt/Clai_TALOS --channel stable
  update_via_curl.sh --target "$HOME/Clai_TALOS" --channel prerelease
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)
      TARGET="${2:-}"
      shift 2
      ;;
    --channel)
      CHANNEL="${2:-}"
      shift 2
      ;;
    --repo)
      REPO="${2:-}"
      shift 2
      ;;
    --branch)
      BRANCH="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$TARGET" ]]; then
  TARGET="$PWD"
fi

case "${CHANNEL,,}" in
  stable|prerelease)
    ;;
  *)
    echo "Invalid --channel value: $CHANNEL (expected stable or prerelease)" >&2
    exit 2
    ;;
esac

if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  echo "Python is required but was not found in PATH." >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required but was not found in PATH." >&2
  exit 1
fi

TMP_DIR="$(mktemp -d 2>/dev/null || mktemp -d -t clai_update)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

UPDATER_URL="https://raw.githubusercontent.com/VynavinV/Clai_TALOS/${BRANCH}/scripts/update_legacy_copy.py"
UPDATER_FILE="$TMP_DIR/update_legacy_copy.py"

echo "[info] Downloading updater from: $UPDATER_URL"
curl -fsSL --retry 3 --connect-timeout 15 "$UPDATER_URL" -o "$UPDATER_FILE"

echo "[info] Running update for target: $TARGET"
"$PYTHON_BIN" "$UPDATER_FILE" --target "$TARGET" --channel "$CHANNEL" --repo "$REPO"

echo "[done] Update finished. Existing configs and runtime data were preserved by the updater."
