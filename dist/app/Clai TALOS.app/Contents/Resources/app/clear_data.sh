#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Clearing all Clai_TALOS user data..."

rm -f "$SCRIPT_DIR/talos.db"
rm -f "$SCRIPT_DIR/talos.db-wal"
rm -f "$SCRIPT_DIR/talos.db-shm"
rm -f "$SCRIPT_DIR/.env"
rm -f "$SCRIPT_DIR/.credentials"
rm -f "$SCRIPT_DIR/.google_oauth.json"
rm -f "$SCRIPT_DIR/.security.log"
rm -f "$SCRIPT_DIR/.setup_config"
rm -f "$SCRIPT_DIR/.tools_config"

rm -rf "$SCRIPT_DIR/.himalaya"
rm -rf "$SCRIPT_DIR/.browser-profile"
rm -rf "$SCRIPT_DIR/projects"
rm -rf "$SCRIPT_DIR/logs"

echo "Done. All user data cleared."
echo "Run onboarding again on next startup."
