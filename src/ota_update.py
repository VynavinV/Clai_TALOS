import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

import app_paths


DEFAULT_GITHUB_REPO = "VynavinV/Clai_TALOS"


def _bool_env(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def _run(args: list[str], *, cwd: str | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _repo_root() -> str:
    return os.path.realpath(os.path.join(app_paths.source_root(), os.pardir))


def _has_git_checkout(path: str) -> bool:
    return os.path.isdir(os.path.join(path, ".git"))


def _git_bin() -> str | None:
    return shutil.which("git")


def current_version() -> str:
    configured = str(os.getenv("TALOS_VERSION", "")).strip()
    if configured:
        return configured

    git = _git_bin()
    repo_root = _repo_root()
    if not git or not _has_git_checkout(repo_root):
        return "unknown"

    try:
        result = _run([git, "-C", repo_root, "rev-parse", "--short", "HEAD"], timeout=10)
        if result.returncode == 0:
            version = result.stdout.strip()
            # Check if dirty
            dirty = _run([git, "-C", repo_root, "diff", "--quiet", "--ignore-submodules", "HEAD"], timeout=10)
            if dirty.returncode != 0:
                version += "-dirty"
            return version
    except Exception:
        pass

    return "unknown"


def _save_rollback_metadata(tag: str, previous_head: str, commit_count: int) -> None:
    updates_dir = app_paths.data_path("updates")
    os.makedirs(updates_dir, exist_ok=True)
    metadata_path = os.path.join(updates_dir, "rollback.json")

    metadata = {
        "tag": tag,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "previous_head": previous_head,
        "commit_count": commit_count,
    }

    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)


def _load_rollback_metadata() -> dict | None:
    metadata_path = app_paths.data_path("updates", "rollback.json")
    if not os.path.isfile(metadata_path):
        return None

    try:
        with open(metadata_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)
        if isinstance(metadata, dict) and "tag" in metadata:
            return metadata
    except Exception:
        pass

    return None


def _create_rollback_tag() -> str:
    git = _git_bin()
    repo_root = _repo_root()
    if not git or not _has_git_checkout(repo_root):
        raise RuntimeError("Git checkout not found")

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    tag = f"talos-rollback-{timestamp}"

    result = _run([git, "-C", repo_root, "tag", "-f", tag, "HEAD"], timeout=10)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to create rollback tag: {result.stderr}")

    return tag


def _delete_rollback_tag(tag: str) -> None:
    git = _git_bin()
    repo_root = _repo_root()
    if not git or not _has_git_checkout(repo_root):
        return

    _run([git, "-C", repo_root, "tag", "-d", tag], timeout=10)


def _health_check(timeout_s: int = 30) -> bool:
    """Best-effort health check.

    Reads OTA_HEALTH_CHECK_URL (default http://localhost:{PORT}/) and
    OTA_HEALTH_CHECK_TIMEOUT_S (overrides timeout_s) from the environment.

    NOTE: When called after _run_clai_restart(), the 'clai restart' command
    typically kills this process, so the health check will not complete in
    the restarting process. This is a known limitation of the restart model.
    """
    import urllib.request
    import urllib.error

    env_timeout = os.getenv("OTA_HEALTH_CHECK_TIMEOUT_S", "").strip()
    if env_timeout.isdigit():
        timeout_s = int(env_timeout)

    port = os.getenv("PORT", "8080")
    url = os.getenv("OTA_HEALTH_CHECK_URL", "").strip()
    if not url:
        url = f"http://localhost:{port}/"
    deadline = time.time() + timeout_s

    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)

    return False


def _run_clai_restart() -> bool:
    clai_bin = shutil.which("clai")
    if not clai_bin:
        return False

    try:
        # Launch clai restart in background so it can restart us
        subprocess.Popen([clai_bin, "restart"], close_fds=True)
        return True
    except Exception:
        return False


def check_for_updates() -> dict:
    enabled = _bool_env("OTA_ENABLED", True)

    base = {
        "ok": True,
        "enabled": enabled,
        "install_mode": "source-git",
        "current_version": current_version(),
        "update_available": False,
        "can_apply": False,
    }

    if not enabled:
        base["message"] = "OTA updates are disabled by OTA_ENABLED=0."
        return base

    git = _git_bin()
    repo_root = _repo_root()
    if not git or not _has_git_checkout(repo_root):
        base["ok"] = False
        base["error"] = "Git checkout not found"
        return base

    # Fetch latest
    try:
        fetch = _run([git, "-C", repo_root, "fetch", "--quiet", "origin"], timeout=60)
        if fetch.returncode != 0:
            base["ok"] = False
            base["error"] = f"git fetch failed: {fetch.stderr}"
            return base
    except Exception as exc:
        base["ok"] = False
        base["error"] = f"git fetch failed: {exc}"
        return base

    # Compare HEAD vs origin/HEAD
    try:
        head = _run([git, "-C", repo_root, "rev-parse", "HEAD"], timeout=10)
        remote_head = _run([git, "-C", repo_root, "rev-parse", "origin/HEAD"], timeout=10)

        if head.returncode != 0 or remote_head.returncode != 0:
            base["ok"] = False
            base["error"] = "Could not determine git HEAD positions"
            return base

        local_commit = head.stdout.strip()
        remote_commit = remote_head.stdout.strip()

        # Count commits behind
        behind = _run([git, "-C", repo_root, "rev-list", "--count", f"{local_commit}..{remote_commit}"], timeout=10)
        behind_count = int(behind.stdout.strip()) if behind.returncode == 0 and behind.stdout.strip().isdigit() else 0

        available = behind_count > 0

        # Get latest commit info
        latest_commit = {}
        if available:
            log = _run([git, "-C", repo_root, "log", "-1", "--format=%H|%s|%an|%ad", remote_commit], timeout=10)
            if log.returncode == 0:
                parts = log.stdout.strip().split("|", 3)
                if len(parts) >= 4:
                    latest_commit = {
                        "sha": parts[0][:8],
                        "message": parts[1],
                        "author": parts[2],
                        "date": parts[3],
                    }

        base.update({
            "update_available": available,
            "can_apply": available,
            "commits_behind": behind_count,
            "latest_version": remote_commit[:8],
            "latest_commit": latest_commit,
            "message": f"{behind_count} new commit(s) available." if available else "Already up to date with remote.",
        })

    except Exception as exc:
        base["ok"] = False
        base["error"] = f"Update check failed: {exc}"

    return base


def apply_update() -> dict:
    status = check_for_updates()
    if not status.get("ok"):
        return status

    if not status.get("enabled", True):
        return {"ok": False, "error": "OTA updates are disabled by OTA_ENABLED=0."}

    if not status.get("update_available"):
        return {"ok": False, "error": "No update is currently available."}

    git = _git_bin()
    repo_root = _repo_root()
    if not git or not _has_git_checkout(repo_root):
        return {"ok": False, "error": "Git checkout not found"}

    # Create rollback tag
    try:
        previous_head = _run([git, "-C", repo_root, "rev-parse", "HEAD"], timeout=10).stdout.strip()
        tag = _create_rollback_tag()
        commit_count = status.get("commits_behind", 0)
        _save_rollback_metadata(tag, previous_head, commit_count)
    except Exception as exc:
        return {"ok": False, "error": f"Failed to create rollback tag: {exc}"}

    # Pull updates
    try:
        pull = _run([git, "-C", repo_root, "pull", "--ff-only", "origin"], timeout=180)
        if pull.returncode != 0:
            return {
                "ok": False,
                "error": f"git pull failed: {pull.stderr}",
                "rollback_tag": tag,
            }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"git pull failed: {exc}",
            "rollback_tag": tag,
        }

    # Install dependencies
    req_path = os.path.join(app_paths.source_root(), "requirements.txt")
    if os.path.isfile(req_path):
        try:
            pip = _run([sys.executable, "-m", "pip", "install", "-r", req_path], timeout=900)
            if pip.returncode != 0:
                return {
                    "ok": False,
                    "error": f"Dependencies update failed: {pip.stderr}",
                    "rollback_tag": tag,
                }
        except Exception as exc:
            return {
                "ok": False,
                "error": f"Dependencies update failed: {exc}",
                "rollback_tag": tag,
            }

    # Restart via clai CLI
    if not _run_clai_restart():
        return {
            "ok": False,
            "error": "Failed to run 'clai restart'",
            "rollback_tag": tag,
            "restart_required": True,
        }

    # Best-effort auto-rollback health check.
    # Limitation: 'clai restart' launches a background process that restarts
    # the service, which typically kills the current process. In that case
    # the code below will not finish executing; the health check only runs
    # to completion if the current process survives the restart (e.g. during
    # testing, or if the restart is deferred). Callers should not rely on
    # this being a complete safety net.
    time.sleep(5)

    env_timeout_raw = os.getenv("OTA_HEALTH_CHECK_TIMEOUT_S", "").strip()
    health_timeout = int(env_timeout_raw) if env_timeout_raw.isdigit() else 30

    if not _health_check(timeout_s=health_timeout):
        rollback_result = rollback_update()
        return {
            "ok": False,
            "error": "Health check failed after restart; attempted rollback",
            "rollback_tag": tag,
            "rollback_result": rollback_result,
        }

    return {
        "ok": True,
        "mode": "source-git",
        "applied": True,
        "restarting_now": True,
        "rollback_tag": tag,
        "message": "Update applied. Restarting now...",
    }


def rollback_update() -> dict:
    # Enforce OTA_ENABLED kill switch: rollback must not run when OTA is disabled.
    if not _bool_env("OTA_ENABLED", True):
        return {"ok": False, "error": "OTA updates are disabled by OTA_ENABLED=0."}

    metadata = _load_rollback_metadata()
    if not metadata:
        return {"ok": False, "error": "No rollback metadata found. Nothing to rollback to."}

    tag = metadata.get("tag")
    if not tag:
        return {"ok": False, "error": "Rollback metadata is missing tag."}

    git = _git_bin()
    repo_root = _repo_root()
    if not git or not _has_git_checkout(repo_root):
        return {"ok": False, "error": "Git checkout not found"}

    # Verify tag exists
    tag_check = _run([git, "-C", repo_root, "tag", "-l", tag], timeout=10)
    if tag_check.returncode != 0 or tag not in tag_check.stdout:
        return {"ok": False, "error": f"Rollback tag '{tag}' not found."}

    # Reset to tag
    try:
        reset = _run([git, "-C", repo_root, "reset", "--hard", tag], timeout=30)
        if reset.returncode != 0:
            return {
                "ok": False,
                "error": f"git reset failed: {reset.stderr}",
            }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"git reset failed: {exc}",
        }

    # Install dependencies
    req_path = os.path.join(app_paths.source_root(), "requirements.txt")
    if os.path.isfile(req_path):
        try:
            pip = _run([sys.executable, "-m", "pip", "install", "-r", req_path], timeout=900)
            if pip.returncode != 0:
                return {
                    "ok": False,
                    "error": f"Dependencies install failed after rollback: {pip.stderr}",
                }
        except Exception as exc:
            return {
                "ok": False,
                "error": f"Dependencies install failed after rollback: {exc}",
            }

    # Restart
    if not _run_clai_restart():
        return {
            "ok": False,
            "error": "Failed to run 'clai restart' after rollback",
            "restart_required": True,
        }

    # Cleanup rollback tag
    _delete_rollback_tag(tag)

    # Remove rollback metadata
    metadata_path = app_paths.data_path("updates", "rollback.json")
    if os.path.isfile(metadata_path):
        try:
            os.remove(metadata_path)
        except Exception:
            pass

    return {
        "ok": True,
        "mode": "source-git",
        "rolled_back": True,
        "restarting_now": True,
        "message": f"Rolled back to {tag}. Restarting now...",
    }
