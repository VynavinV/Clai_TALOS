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
