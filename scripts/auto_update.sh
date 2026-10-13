#!/bin/bash
# Auto-update runner — called by cron at 1 AM.
# Toggle with: clai auto-update enable|disable|status

set -u

CONFIG_FILE="${CLAI_CONFIG:-$HOME/.config/clai-talos/env}"
[[ -f "$CONFIG_FILE" ]] || { echo "No config at $CONFIG_FILE"; exit 1; }
source "$CONFIG_FILE"

if [[ "${AUTO_UPDATE:-false}" != "true" ]]; then
  exit 0
fi

INSTALL_DIR="${INSTALL_DIR:-}"
SRC_DIR="$INSTALL_DIR/src"
PY="$SRC_DIR/venv/bin/python"
LOG_DIR="$SRC_DIR/logs"
LOG_FILE="$LOG_DIR/auto_update.log"

mkdir -p "$LOG_DIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"; }

[[ -x "$PY" ]] || { log "ERROR: python not found at $PY"; exit 1; }

log "Auto-update check starting"

result=$(cd "$SRC_DIR" && "$PY" -c "
import json, ota_update
check = ota_update.check_for_updates()
if not check.get('ok') or not check.get('update_available'):
    print(json.dumps({'updated': False, 'msg': check.get('message', 'up to date')}))
    raise SystemExit(0)
applied = ota_update.apply_update()
print(json.dumps(applied))
" 2>&1) || true

log "$result"
