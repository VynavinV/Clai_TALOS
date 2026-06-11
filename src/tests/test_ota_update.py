import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ota_update


def test_parse_github_repo_from_https_remote():
    remote = "https://github.com/example-org/example-repo.git"
    assert ota_update.parse_github_repo(remote) == "example-org/example-repo"


def test_parse_github_repo_from_ssh_remote():
    remote = "git@github.com:example-org/example-repo.git"
    assert ota_update.parse_github_repo(remote) == "example-org/example-repo"


def test_version_is_newer_for_semver_tags():
    assert ota_update._version_is_newer("v1.2.3", "v1.2.4") is True


def test_version_is_not_newer_when_same():
    assert ota_update._version_is_newer("20260407-abc", "20260407-abc") is False


def test_pick_windows_asset_prefers_latest_alias():
    assets = [
        {"name": "random.zip", "url": "https://example.invalid/random.zip"},
        {
            "name": "ClaiTALOS-windows-x64-latest.zip",
            "url": "https://example.invalid/latest.zip",
        },
        {
            "name": "ClaiTALOS-windows-x64.zip",
            "url": "https://example.invalid/versioned.zip",
        },
    ]

    selected = ota_update._pick_windows_asset(assets)
    assert selected is not None
    assert selected["name"] == "ClaiTALOS-windows-x64-latest.zip"


def test_normalize_channel_aliases_to_prerelease():
    assert ota_update._normalize_channel("preview") == "prerelease"
    assert ota_update._normalize_channel("beta") == "prerelease"
    assert ota_update._normalize_channel("stable") == "stable"


def test_pick_release_from_list_respects_stable_channel():
    releases = [
        {"draft": False, "prerelease": True, "tag_name": "v2.0.0-rc1"},
        {"draft": False, "prerelease": False, "tag_name": "v1.9.0"},
    ]

    stable = ota_update._pick_release_from_list(releases, "stable")
    prerelease = ota_update._pick_release_from_list(releases, "prerelease")

    assert stable is not None
    assert stable["tag_name"] == "v1.9.0"
    assert prerelease is not None
    assert prerelease["tag_name"] == "v2.0.0-rc1"
