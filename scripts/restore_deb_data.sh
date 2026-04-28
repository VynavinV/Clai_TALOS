#!/usr/bin/env bash
set -euo pipefail

# Restore Clai TALOS data after .deb reinstall
# Usage: sudo bash restore_deb_data.sh /path/to/backup/folder

BACKUP_DIR="${1:-.}"
DATA_DIR="/var/lib/clai-talos"
USER_NAME="clai-talos"
GROUP_NAME="clai-talos"

if [[ ! -d "$BACKUP_DIR" ]]; then
  echo "[fail] Backup directory not found: $BACKUP_DIR" >&2
  exit 1
fi

echo "[info] Stopping clai-talos service..."
sudo systemctl stop clai-talos || true

echo "[info] Restoring data to $DATA_DIR..."
mkdir -p "$DATA_DIR"

# Restore individual files and directories
for item in talos.db talos.db-wal talos.db-shm .env .credentials .google_oauth.json .setup_config .tools_config .himalaya projects logs; do
  if [[ -e "$BACKUP_DIR/$item" ]]; then
    echo "[info] Restoring $item..."
    sudo cp -r "$BACKUP_DIR/$item" "$DATA_DIR/" 2>/dev/null || true
  fi
done

echo "[info] Setting ownership..."
sudo chown -R "$USER_NAME:$GROUP_NAME" "$DATA_DIR"

echo "[info] Starting clai-talos service..."
sudo systemctl start clai-talos

echo "[ok] Restore complete. Checking service status..."
sudo systemctl status clai-talos --no-pager

echo "[next] Verify data at: http://localhost:8080"
