# OTA Update System v2 — Design Spec

**Date:** 2026-09-05  
**Status:** Approved for implementation

## Overview

Complete rewrite of the OTA (Over-The-Air) update system for Clai TALOS. The new system is **source-git only**, leveraging `git pull` + `clai restart` as the update mechanism. Includes rollback capability, Telegram bot integration, AI agent tools, and periodic auto-check via cron.

## Goals

- **Simplicity**: Source-git mode only — strip out broken deb/windows/source modes
- **Reliability**: Use existing `clai restart` CLI command for process management
- **Safety**: Rollback capability via git tags, health checks after restart
- **Integration**: Telegram commands, AI tools, cron-based auto-check
- **Security**: SHA256 verification, `OTA_ENABLED` kill switch, auth/CSRF on APIs

## Architecture

### Install Mode
- **Single mode**: `source-git` (detected via `.git` directory)
- All other modes (linux-deb, frozen-windows, source) are **removed**

### Core Operations

#### 1. Check for Updates
```python
def check_for_updates() -> dict:
    # 1. Verify OTA_ENABLED=1
    # 2. git fetch --quiet origin
    # 3. Compare git rev-parse HEAD vs git rev-parse origin/HEAD
    # 4. If behind, return:
    #    - update_available: true
    #    - commits_behind: N
    #    - latest_commit: {sha, message, author, date}
    #    - current_version: short SHA
    #    - latest_version: short SHA
```

#### 2. Apply Update
```python
def apply_update() -> dict:
    # 1. Check for updates
    # 2. Create rollback tag: git tag -f talos-rollback-<timestamp> HEAD
    # 3. Save rollback metadata to data/updates/rollback.json:
    #    {
    #      "tag": "talos-rollback-20260905-143022",
    #      "timestamp": "2026-09-05T14:30:22Z",
    #      "previous_head": "abc1234",
    #      "commit_count": 3
    #    }
    # 4. git pull --ff-only origin <branch>
    # 5. Run: clai restart
    # 6. Wait 5 seconds, then health check (GET /api/health or similar)
    # 7. If health check fails within 30s, auto-rollback
    # 8. Return success with rollback info
```

#### 3. Rollback
```python
def rollback_update() -> dict:
    # 1. Read data/updates/rollback.json
    # 2. Verify rollback tag exists: git tag -l <tag>
    # 3. git reset --hard <tag>
    # 4. Run: clai restart
    # 5. Delete tag: git tag -d <tag>
    # 6. Remove rollback.json
    # 7. Return success
```

### Safety Mechanisms

1. **`git pull --ff-only`**: Fails safely if local changes conflict with remote
2. **Rollback tag**: Created before any destructive operation
3. **Health check**: After restart, verify process is alive (configurable timeout, default 30s)
4. **Auto-rollback**: If health check fails, automatically rollback to previous version
5. **`OTA_ENABLED=0`**: Kill switch to disable all OTA operations
6. **Auth + CSRF**: All API endpoints require authentication
7. **SHA256 verification**: For any downloaded artifacts (though git mode uses git's own integrity checks)

### Integration Points

#### Web Dashboard
- **Existing endpoints** (kept as-is):
  - `GET /api/updates/check` → calls `ota_update.check_for_updates()`
  - `POST /api/updates/apply` → calls `ota_update.apply_update()`
- **New endpoint**:
  - `POST /api/updates/rollback` → calls `ota_update.rollback_update()`
- **UI updates**:
  - Add "Rollback" button (enabled when rollback.json exists)
  - Show rollback tag info and timestamp
  - Health check status indicator during apply

#### Telegram Bot
- **New commands**:
  - `/checkupdate` — check for updates, reply with status
  - `/update` — apply update, reply with progress + result
  - `/rollback` — rollback to previous version, reply with result
- Commands restricted to bot owner/admin (check existing auth pattern)

#### AI Agent Tools
- **New tools** in `src/tools/`:
  - `check_for_update()` — returns update status
  - `apply_update()` — applies update, returns result
  - `rollback_update()` — rolls back, returns result
- Tools should include safety warnings in descriptions
- AI should confirm with user before applying/rolling back

#### Cron Scheduler
- **New cron job** (optional, user-configurable):
  - `OTA_AUTO_CHECK_INTERVAL` env var (default: disabled, or "0 */6 * * *" for every 6 hours)
  - When update available, notify via Telegram (if `OTA_AUTO_NOTIFY=1`)
  - Optional: `OTA_AUTO_APPLY=1` to auto-apply without confirmation (default: 0, requires manual apply)

### Configuration (Environment Variables)

```bash
# Core
OTA_ENABLED=1                    # 0 to disable all OTA operations
OTA_CHANNEL=stable               # stable or prerelease (for GitHub releases if re-added later)
OTA_GITHUB_REPO=VynavinV/Clai_TALOS
OTA_GITHUB_TOKEN=                # optional, for authenticated API requests
OTA_TIMEOUT_S=15                 # HTTP timeout

# Auto-check
OTA_AUTO_CHECK_INTERVAL=         # cron expression, e.g., "0 */6 * * *" (every 6 hours)
OTA_AUTO_NOTIFY=1                # 1 to notify via Telegram when update available
OTA_AUTO_APPLY=0                 # 1 to auto-apply without confirmation (dangerous)

# Rollback
OTA_HEALTH_CHECK_TIMEOUT_S=30    # how long to wait for health check after restart
OTA_HEALTH_CHECK_URL=            # override health check URL (default: http://localhost:<port>/api/health)

# Version override
TALOS_VERSION=                   # override reported version string
```

### File Structure

```
src/
  ota_update.py                  # Core OTA logic (rewrite)
  tests/
    test_ota_update.py           # Unit tests
  tools/
    ota.py                       # AI agent tools (new)
  telegram_bot.py                # Add /update, /checkupdate, /rollback commands
  web/
    settings.html                # Add rollback button to OTA section
  cron_jobs.py                   # Add OTA auto-check job
  app_paths.py                   # data_path("updates") for rollback.json
```

### Rollback Metadata (`data/updates/rollback.json`)

```json
{
  "tag": "talos-rollback-20260905-143022",
  "timestamp": "2026-09-05T14:30:22Z",
  "previous_head": "abc1234",
  "commit_count": 3,
  "latest_commit": {
    "sha": "def5678",
    "message": "Add new feature",
    "author": "Alice",
    "date": "2026-09-05T14:00:00Z"
  }
}
```

### Health Check

After `clai restart`, the system waits 5 seconds, then polls the health endpoint:
- **Default URL**: `http://localhost:<port>/api/health` (port from env or config)
- **Timeout**: 30 seconds (configurable via `OTA_HEALTH_CHECK_TIMEOUT_S`)
- **Interval**: Poll every 2 seconds
- **Success**: HTTP 200 response
- **Failure**: If timeout reached, trigger auto-rollback

### Error Handling

All operations return structured results:
```python
{
  "ok": True/False,
  "error": "error message if not ok",
  "mode": "source-git",
  "current_version": "abc1234",
  "latest_version": "def5678",
  "update_available": True/False,
  "rollback_tag": "talos-rollback-20260905-143022",
  "rollback_available": True/False,
  "message": "human-readable status"
}
```

### Testing Strategy

1. **Unit tests** (`src/tests/test_ota_update.py`):
   - Version comparison logic
   - Rollback metadata parsing/writing
   - Git command parsing (mock subprocess)
   - Health check logic (mock HTTP requests)

2. **Integration tests**:
   - Simulate git pull with local test repo
   - Verify rollback tag creation/deletion
   - Test health check timeout and auto-rollback

3. **Manual tests**:
   - Apply update on test instance
   - Verify `clai restart` works
   - Trigger rollback manually
   - Test Telegram commands
   - Test AI agent tools

## Implementation Plan

### Phase 1: Core OTA Rewrite
1. Rewrite `src/ota_update.py` with source-git-only logic
2. Implement `check_for_updates()`, `apply_update()`, `rollback_update()`
3. Add rollback metadata management
4. Add health check logic
5. Update `src/app_paths.py` if needed for `data/updates` path

### Phase 2: Web Dashboard Integration
1. Update `src/telegram_bot.py` endpoints:
   - Keep `/api/updates/check` and `/api/updates/apply`
   - Add `/api/updates/rollback` endpoint
2. Update `src/web/settings.html`:
   - Add rollback button (enabled when rollback.json exists)
   - Show rollback info (tag, timestamp, commit count)
   - Add health check status during apply

### Phase 3: Telegram Bot Commands
1. Add `/checkupdate` command in `src/telegram_bot.py`
2. Add `/update` command
3. Add `/rollback` command
4. Restrict to bot owner/admin

### Phase 4: AI Agent Tools
1. Create `src/tools/ota.py` with tool definitions
2. Register tools in tool registry
3. Add safety warnings to tool descriptions

### Phase 5: Cron Auto-Check
1. Add OTA auto-check job in `src/cron_jobs.py`
2. Implement notification logic (Telegram message when update available)
3. Optional: auto-apply if `OTA_AUTO_APPLY=1`

### Phase 6: Testing & Documentation
1. Write unit tests in `src/tests/test_ota_update.py`
2. Test all integration points (web, Telegram, AI, cron)
3. Update README with new OTA features and configuration

## Migration Notes

- **Existing `ota_update.py`**: Complete rewrite — old deb/windows logic removed
- **Existing web UI**: Keep structure, add rollback button
- **Existing API endpoints**: Keep URLs, update implementation
- **Environment variables**: Most existing vars kept, new ones added for auto-check and health check
- **Legacy scripts**: `scripts/update_legacy_copy.py` and `scripts/update_via_curl.sh` can remain for pre-OTA installations (unchanged)

## Security Considerations

1. **Git integrity**: Git itself verifies commit integrity via SHA-1 hashes
2. **Rollback tag**: Only created locally, not pushed to remote
3. **Health check**: Local HTTP request, no external dependencies
4. **Auth/CSRF**: All API endpoints require authentication (existing pattern)
5. **Telegram commands**: Restricted to bot owner/admin (existing pattern)
6. **AI tools**: Descriptions include safety warnings, AI should confirm with user

## Future Enhancements (Out of Scope)

- Docker-based updates (`docker pull` + container restart)
- Delta updates (only download changed files)
- Staged rollouts (percentage-based deployment)
- Update notifications via email/webhook
- Multi-repo support (updates from different remotes)
- Custom update servers (self-hosted release endpoints)
