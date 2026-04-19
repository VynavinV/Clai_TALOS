#!/usr/bin/env python3
"""
Upgrade an existing Clai TALOS copy to the latest release, including OTA support.

This is intended for older copied installs that do not yet include OTA update code.
It works in two modes:
- git checkout target: run `git pull --ff-only`
- non-git copied target: download latest source archive and overlay code files

Runtime/user data is preserved (for example src/.env, src/.credentials, src/logs, src/projects, db files).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_GITHUB_REPO = "vynavinv/clai-talos"
DEFAULT_RELEASES_URL_TEMPLATE = "https://api.github.com/repos/{repo}/releases/latest"
DEFAULT_RELEASES_LIST_URL_TEMPLATE = "https://api.github.com/repos/{repo}/releases?per_page=30"

SYNC_TOP_LEVEL_ITEMS = [
    "src",
    "scripts",
    "start.bat",
    "start.sh",
    "README.md",
    "LICENSE",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
]

EXCLUDE_SRC_TOP = {
    "venv",
    ".venv",
    "build",
    "dist",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
}

PRESERVE_SRC_RUNTIME = {
    ".env",
    ".credentials",
    ".security.log",
    ".setup_config",
    ".tools_config",
    ".google_oauth.json",
    "talos.db",
    "talos.db-wal",
    "talos.db-shm",
    "terminal_config.json",
    "projects",
    "logs",
    "bin",
    ".himalaya",
    "community_hub",
}


def _normalize_channel(channel: str) -> str:
    value = str(channel or "").strip().lower()
    if value in {"pre", "preview", "prerelease", "pre-release", "beta"}:
        return "prerelease"
    return "stable"


def _http_json(url: str) -> object:
    req = Request(
        url,
        headers={
            "User-Agent": "Clai-TALOS-Legacy-Updater",
            "Accept": "application/vnd.github+json",
        },
    )
    with urlopen(req, timeout=20) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8", errors="replace"))


def _pick_release_from_list(items: list[dict], channel: str) -> dict | None:
    selected_channel = _normalize_channel(channel)
    for item in items:
        if not isinstance(item, dict):
            continue
        if bool(item.get("draft", False)):
            continue
        if selected_channel == "stable" and bool(item.get("prerelease", False)):
            continue
        return item
    return None


def _fetch_release(repo: str, channel: str) -> dict:
    selected_channel = _normalize_channel(channel)

    try:
        if selected_channel == "stable":
            url = DEFAULT_RELEASES_URL_TEMPLATE.format(repo=repo)
            payload = _http_json(url)
            if not isinstance(payload, dict):
                raise RuntimeError("Release API returned unexpected payload")
            release = payload
        else:
            url = DEFAULT_RELEASES_LIST_URL_TEMPLATE.format(repo=repo)
            payload = _http_json(url)
            if not isinstance(payload, list):
                raise RuntimeError("Release-list API returned unexpected payload")
            release = _pick_release_from_list(payload, selected_channel)
            if not release:
                raise RuntimeError("No release found for selected channel")
    except HTTPError as exc:
        raise RuntimeError(f"Release API request failed with HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach release API: {exc.reason}") from exc

    zipball_url = str(release.get("zipball_url", "")).strip()
    if not zipball_url:
        raise RuntimeError("Release metadata missing zipball_url")

    return {
        "channel": selected_channel,
        "tag_name": str(release.get("tag_name", "")).strip(),
        "name": str(release.get("name", "")).strip(),
        "url": str(release.get("html_url", "")).strip(),
        "zipball_url": zipball_url,
        "prerelease": bool(release.get("prerelease", False)),
    }


def _download_file(url: str, destination: Path) -> None:
    req = Request(url, headers={"User-Agent": "Clai-TALOS-Legacy-Updater"})
    with urlopen(req, timeout=60) as resp, destination.open("wb") as out:
        while True:
            chunk = resp.read(128 * 1024)
            if not chunk:
                break
            out.write(chunk)


def _extract_zip(archive_path: Path, extract_dir: Path) -> Path:
    with zipfile.ZipFile(archive_path, "r") as zf:
        zf.extractall(extract_dir)

    roots = [p for p in extract_dir.iterdir() if p.is_dir()]
    if len(roots) != 1:
        raise RuntimeError("Expected one root directory in extracted archive")
    return roots[0]


def _copy_file_or_tree(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _overlay_src_tree(source_src_dir: Path, target_src_dir: Path) -> None:
    target_src_dir.mkdir(parents=True, exist_ok=True)

    for child in source_src_dir.iterdir():
        if child.name in EXCLUDE_SRC_TOP:
            continue
        if child.name in PRESERVE_SRC_RUNTIME:
            continue
        _copy_file_or_tree(child, target_src_dir / child.name)


def _overlay_non_git_copy(extracted_root: Path, target_root: Path) -> None:
    for item_name in SYNC_TOP_LEVEL_ITEMS:
        src_item = extracted_root / item_name
        if not src_item.exists():
            continue

        dst_item = target_root / item_name
        if item_name == "src" and src_item.is_dir():
            _overlay_src_tree(src_item, dst_item)
            continue

        _copy_file_or_tree(src_item, dst_item)


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _target_python(target_root: Path) -> str:
    candidates = [
        target_root / "src" / "venv" / "Scripts" / "python.exe",
        target_root / "src" / ".venv" / "Scripts" / "python.exe",
        target_root / "src" / "venv" / "bin" / "python",
        target_root / "src" / ".venv" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def _install_requirements(target_root: Path) -> None:
    req = target_root / "src" / "requirements.txt"
    if not req.is_file():
        return

    python_cmd = _target_python(target_root)
    result = _run([python_cmd, "-m", "pip", "install", "-r", str(req)], timeout=1200)
    if result.returncode != 0:
        detail = (result.stderr or "") + "\n" + (result.stdout or "")
        raise RuntimeError(f"Dependency install failed:\n{detail.strip()}")


def _update_git_target(target_root: Path) -> None:
    git_bin = shutil.which("git")
    if not git_bin:
        raise RuntimeError("git is not installed")

    pull = _run([git_bin, "-C", str(target_root), "pull", "--ff-only"], timeout=300)
    if pull.returncode != 0:
        detail = (pull.stderr or "") + "\n" + (pull.stdout or "")
        raise RuntimeError(f"git pull failed:\n{detail.strip()}")


def run_update(target_root: Path, channel: str, repo: str) -> None:
    if not (target_root / "src").is_dir():
        raise RuntimeError(f"Target folder does not look like a Clai TALOS copy: {target_root}")

    if (target_root / ".git").is_dir():
        print("[info] Detected git checkout; updating with git pull...")
        _update_git_target(target_root)
    else:
        print("[info] Detected non-git copy; downloading release source archive...")
        release = _fetch_release(repo=repo, channel=channel)
        print(f"[info] Selected release: {release.get('tag_name') or release.get('name')}")
        print(f"[info] Channel: {release.get('channel')}")

        with tempfile.TemporaryDirectory(prefix="talos_legacy_update_") as tmp:
            tmp_dir = Path(tmp)
            archive_path = tmp_dir / "release.zip"
            _download_file(release["zipball_url"], archive_path)
            extracted_root = _extract_zip(archive_path, tmp_dir / "extract")
            _overlay_non_git_copy(extracted_root, target_root)

    print("[info] Installing/updating Python dependencies...")
    _install_requirements(target_root)
    print("[done] Update complete.")
    print("[next] Restart TALOS in that target copy to run the updated code.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update legacy Clai TALOS copied installs to OTA-capable version.")
    parser.add_argument(
        "--target",
        default=".",
        help="Path to the Clai TALOS copy to update (default: current directory)",
    )
    parser.add_argument(
        "--channel",
        default="stable",
        choices=["stable", "prerelease"],
        help="Release channel to use (default: stable)",
    )
    parser.add_argument(
        "--repo",
        default=DEFAULT_GITHUB_REPO,
        help="GitHub repo in owner/name format (default: vynavinv/clai-talos)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    target_root = Path(args.target).expanduser().resolve()

    if not target_root.exists():
        print(f"[fail] Target path does not exist: {target_root}")
        return 1

    try:
        run_update(target_root=target_root, channel=args.channel, repo=args.repo)
    except Exception as exc:
        print(f"[fail] {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
