# OTA Updates

Over-the-air update management via git. Check for updates, apply them with automatic rollback capability, or rollback to the previous version.

## check_for_update

Check if OTA updates are available from the git remote.

**Parameters:** None

**Returns:** JSON object with:
- `ok`: boolean — whether the check succeeded
- `enabled`: boolean — whether OTA updates are enabled (OTA_ENABLED env var)
- `install_mode`: string — always "source-git"
- `current_version`: string — current git SHA (short) or "unknown"
- `update_available`: boolean — true if remote has newer commits
- `can_apply`: boolean — true if update can be applied
- `commits_behind`: integer — number of commits behind remote
- `latest_version`: string — latest git SHA (short)
- `latest_commit`: object — commit details (sha, message, author, date)
- `message`: string — human-readable status

**Example:**
```json
{
  "tool": "check_for_update",
  "parameters": {}
}
```

**Safety:** Read-only operation. Does not modify the system.

---

## apply_update

Apply OTA update by pulling latest code from git and restarting the bot. Creates a rollback tag before applying.

**Parameters:** None

**Returns:** JSON object with:
- `ok`: boolean — whether the update succeeded
- `rollback_tag`: string — git tag name for rollback (e.g., "talos-rollback-20260905-143022")
- `restarting_now`: boolean — true if bot is restarting now
- `message`: string — human-readable status
- `restart_required`: boolean — true if manual restart needed

**Example:**
```json
{
  "tool": "apply_update",
  "parameters": {}
}
```

**Safety:** ⚠️ **Destructive operation.** This will:
1. Create a rollback tag at current HEAD
2. Pull latest code from git remote (fast-forward only)
3. Install Python dependencies (pip install -r requirements.txt)
4. Restart the bot via `clai restart`
5. Best-effort health check with auto-rollback if it fails

**Always confirm with the user before applying.** The bot will be unavailable for ~15 seconds during restart.

**Environment variables:**
- `OTA_ENABLED` — set to "0" or "false" to disable all OTA operations
- `OTA_HEALTH_CHECK_URL` — URL to check after restart (default: http://localhost:{PORT}/)
- `OTA_HEALTH_CHECK_TIMEOUT_S` — health check timeout in seconds (default: 30)

---

## rollback_update

Rollback to the previous version using the rollback tag created during the last update.

**Parameters:** None

**Returns:** JSON object with:
- `ok`: boolean — whether the rollback succeeded
- `restarting_now`: boolean — true if bot is restarting now
- `message`: string — human-readable status
- `restart_required`: boolean — true if manual restart needed

**Example:**
```json
{
  "tool": "rollback_update",
  "parameters": {}
}
```

**Safety:** ⚠️ **Destructive operation.** This will:
1. Verify rollback tag exists (created by last `apply_update`)
2. Reset git HEAD to the rollback tag (hard reset)
3. Reinstall Python dependencies
4. Restart the bot via `clai restart`
5. Delete the rollback tag and metadata

**Always confirm with the user before rolling back.** Use this when an update caused issues.

**Limitations:**
- Rollback only works if an update was previously applied (rollback tag exists)
- After rollback, the rollback tag is deleted — you cannot rollback again
- Any uncommitted changes will be lost during rollback
