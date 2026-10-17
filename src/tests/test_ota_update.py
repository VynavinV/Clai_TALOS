import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ota_update


def test_current_version_returns_git_sha():
    # Mock git command to return "abc1234"
    # For now, just verify function exists and returns string
    version = ota_update.current_version()
    assert isinstance(version, str)
    assert len(version) > 0


def test_save_and_load_rollback_metadata(tmp_path, monkeypatch):
    # Mock app_paths.data_path to use tmp_path
    import app_paths
    monkeypatch.setattr(app_paths, "data_path", lambda *parts: str(tmp_path.joinpath(*parts)))

    tag = "talos-rollback-20260905-143022"
    previous_head = "abc1234"
    commit_count = 3

    ota_update._save_rollback_metadata(tag, previous_head, commit_count)
    metadata = ota_update._load_rollback_metadata()

    assert metadata is not None
    assert metadata["tag"] == tag
    assert metadata["previous_head"] == previous_head
    assert metadata["commit_count"] == commit_count


def test_load_rollback_metadata_returns_none_when_missing(tmp_path, monkeypatch):
    import app_paths
    monkeypatch.setattr(app_paths, "data_path", lambda *parts: str(tmp_path.joinpath(*parts)))

    metadata = ota_update._load_rollback_metadata()
    assert metadata is None


def _fake_apply_env(monkeypatch, restart_calls):
    """Wire apply_update's git/deps plumbing to succeed without touching disk."""
    import subprocess

    monkeypatch.setattr(
        ota_update,
        "check_for_updates",
        lambda: {
            "ok": True,
            "enabled": True,
            "update_available": True,
            "can_apply": True,
            "commits_behind": 2,
        },
    )
    monkeypatch.setattr(ota_update, "_git_bin", lambda: "/usr/bin/git")
    monkeypatch.setattr(ota_update, "_has_git_checkout", lambda path: True)
    monkeypatch.setattr(ota_update, "_create_rollback_tag", lambda: "talos-rollback-test")
    monkeypatch.setattr(ota_update, "_save_rollback_metadata", lambda *a: None)

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    monkeypatch.setattr(ota_update, "_run", fake_run)

    def fake_restart():
        restart_calls.append(1)
        return True

    monkeypatch.setattr(ota_update, "_run_clai_restart", fake_restart)
    monkeypatch.setattr(ota_update, "_health_check", lambda timeout_s=30: True)


def test_apply_update_deferred_does_not_restart(monkeypatch):
    """The web handler must get its response out before any restart happens."""
    restart_calls = []
    _fake_apply_env(monkeypatch, restart_calls)

    result = ota_update.apply_update(defer_restart=True)

    assert result["ok"] is True
    assert result["restart_scheduled"] is True
    assert result["applied"] is True
    # No restart was started: the caller schedules it after the response flush.
    assert restart_calls == []


def test_apply_update_synchronous_restarts(monkeypatch):
    restart_calls = []
    _fake_apply_env(monkeypatch, restart_calls)

    result = ota_update.apply_update(defer_restart=False)

    assert result["ok"] is True
    assert result.get("restarting_now") is True
    assert restart_calls == [1]


def test_schedule_restart_uses_detached_shell(monkeypatch):
    """schedule_restart launches a detached sleeper so the response can flush."""
    popped = []

    class _FakePopen:
        def __init__(self, args, **kwargs):
            popped.append((args, kwargs))

    monkeypatch.setattr(ota_update.shutil, "which", lambda name: "/usr/local/bin/clai")
    monkeypatch.setattr(ota_update.subprocess, "Popen", _FakePopen)

    assert ota_update.schedule_restart(delay_s=2.0) is True
    args, kwargs = popped[0]
    assert args[0] == "/bin/sh"
    assert args[1] == "-c"
    assert "sleep 2.0" in args[2]
    assert args[3] == "/usr/local/bin/clai"
    assert kwargs.get("start_new_session") is True


def test_schedule_restart_false_without_clai(monkeypatch):
    monkeypatch.setattr(ota_update.shutil, "which", lambda name: None)
    assert ota_update.schedule_restart() is False
