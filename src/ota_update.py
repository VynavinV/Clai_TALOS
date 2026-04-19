import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import app_paths


DEFAULT_GITHUB_REPO = "vynavinv/clai-talos"
DEFAULT_WINDOWS_ASSET_NAME = "ClaiTALOS-windows-x64-latest.zip"
DEFAULT_RELEASES_URL_TEMPLATE = "https://api.github.com/repos/{repo}/releases/latest"
DEFAULT_RELEASES_LIST_URL_TEMPLATE = "https://api.github.com/repos/{repo}/releases?per_page=30"
DEFAULT_TIMEOUT_S = 15

_GITHUB_REPO_RE = re.compile(r"github\.com[:/](?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?$")


def _bool_env(name: str, default: bool) -> bool:
	raw = str(os.getenv(name, "")).strip().lower()
	if not raw:
		return default
	return raw not in {"0", "false", "no", "off"}


def _safe_filename(name: str, fallback: str = "update.zip") -> str:
	cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", str(name or "").strip())
	if not cleaned:
		cleaned = fallback
	return cleaned[:180]


def _clip(text: str, limit: int = 1600) -> str:
	data = str(text or "").strip()
	if len(data) <= limit:
		return data
	return data[:limit] + "..."


def _normalize_channel(channel: str) -> str:
	value = str(channel or "").strip().lower()
	if value in {"pre", "preview", "prerelease", "pre-release", "beta"}:
		return "prerelease"
	return "stable"


def _repo_root() -> str:
	return os.path.realpath(os.path.join(app_paths.source_root(), os.pardir))


def _has_git_checkout(path: str) -> bool:
	return os.path.isdir(os.path.join(path, ".git"))


def parse_github_repo(remote_url: str) -> str | None:
	candidate = str(remote_url or "").strip()
	if not candidate:
		return None
	match = _GITHUB_REPO_RE.search(candidate)
	if not match:
		return None
	owner = match.group("owner")
	repo = match.group("repo")
	if not owner or not repo:
		return None
	return f"{owner}/{repo}"


def _run(
	args: list[str],
	*,
	cwd: str | None = None,
	timeout: int = 60,
) -> subprocess.CompletedProcess:
	return subprocess.run(
		args,
		cwd=cwd,
		capture_output=True,
		text=True,
		timeout=timeout,
	)


def _git_remote_repo(repo_root: str) -> str | None:
	git_bin = shutil.which("git")
	if not git_bin or not _has_git_checkout(repo_root):
		return None

	try:
		result = _run([git_bin, "-C", repo_root, "config", "--get", "remote.origin.url"], timeout=10)
	except Exception:
		return None

	if result.returncode != 0:
		return None
	return parse_github_repo(result.stdout)


def _resolve_github_repo() -> str:
	configured = str(os.getenv("OTA_GITHUB_REPO", "")).strip()
	if configured:
		return configured

	discovered = _git_remote_repo(_repo_root())
	if discovered:
		return discovered

	return DEFAULT_GITHUB_REPO


def _releases_api_url(repo: str) -> str:
	override = str(os.getenv("OTA_RELEASES_API_URL", "")).strip()
	if override:
		return override
	return DEFAULT_RELEASES_URL_TEMPLATE.format(repo=repo)


def _releases_list_api_url(repo: str) -> str:
	override = str(os.getenv("OTA_RELEASES_LIST_API_URL", "")).strip()
	if override:
		return override
	return DEFAULT_RELEASES_LIST_URL_TEMPLATE.format(repo=repo)


def _http_bytes(url: str, timeout_s: int = DEFAULT_TIMEOUT_S) -> bytes:
	req = Request(
		url,
		headers={
			"User-Agent": "Clai-TALOS-OTA",
			"Accept": "application/vnd.github+json",
		},
	)
	with urlopen(req, timeout=timeout_s) as resp:
		return resp.read()


def _http_json(url: str, timeout_s: int = DEFAULT_TIMEOUT_S) -> dict:
	raw = _http_bytes(url, timeout_s=timeout_s)
	data = json.loads(raw.decode("utf-8", errors="replace"))
	if not isinstance(data, dict):
		raise ValueError("Expected JSON object")
	return data


def _http_json_any(url: str, timeout_s: int = DEFAULT_TIMEOUT_S):
	raw = _http_bytes(url, timeout_s=timeout_s)
	return json.loads(raw.decode("utf-8", errors="replace"))


def _download_to_path(url: str, destination: str, timeout_s: int = DEFAULT_TIMEOUT_S) -> None:
	req = Request(url, headers={"User-Agent": "Clai-TALOS-OTA"})
	os.makedirs(os.path.dirname(destination), exist_ok=True)
	with urlopen(req, timeout=timeout_s) as resp, open(destination, "wb") as out:
		while True:
			chunk = resp.read(1024 * 128)
			if not chunk:
				break
			out.write(chunk)


def _sha256(path: str) -> str:
	digest = hashlib.sha256()
	with open(path, "rb") as f:
		for chunk in iter(lambda: f.read(1024 * 1024), b""):
			digest.update(chunk)
	return digest.hexdigest().lower()


def _find_expected_digest(checksums_text: str, asset_name: str) -> str | None:
	target = str(asset_name or "").strip()
	if not target:
		return None

	for line in checksums_text.splitlines():
		cleaned = line.strip()
		if not cleaned or cleaned.startswith("#"):
			continue

		parts = cleaned.split()
		if len(parts) < 2:
			continue

		digest = parts[0].strip().lower()
		file_name = parts[-1].lstrip("* ")
		if file_name == target and re.fullmatch(r"[a-f0-9]{64}", digest):
			return digest
	return None


def _windows_manifest_candidates() -> list[str]:
	exe_dir = app_paths.executable_dir()
	resource_dir = app_paths.resource_root()
	source_dir = app_paths.source_root()
	return [
		os.path.join(exe_dir, "src", "dist", "build-manifest.json"),
		os.path.join(exe_dir, "_internal", "src", "dist", "build-manifest.json"),
		os.path.join(resource_dir, "src", "dist", "build-manifest.json"),
		os.path.join(source_dir, "dist", "build-manifest.json"),
	]


def _manifest_version() -> str | None:
	for candidate in _windows_manifest_candidates():
		if not os.path.isfile(candidate):
			continue
		try:
			with open(candidate, "r", encoding="utf-8") as f:
				payload = json.load(f)
		except Exception:
			continue

		if isinstance(payload, list) and payload:
			first = payload[0]
			if isinstance(first, dict):
				version = str(first.get("version", "")).strip()
				if version:
					return version

		if isinstance(payload, dict):
			version = str(payload.get("version", "")).strip()
			if version:
				return version

	return None


def _git_local_version(repo_root: str) -> str | None:
	git_bin = shutil.which("git")
	if not git_bin or not _has_git_checkout(repo_root):
		return None

	try:
		sha = _run([git_bin, "-C", repo_root, "rev-parse", "--short", "HEAD"], timeout=10)
	except Exception:
		return None

	if sha.returncode != 0:
		return None

	value = sha.stdout.strip()
	if not value:
		return None

	try:
		dirty = _run([git_bin, "-C", repo_root, "diff", "--quiet", "--ignore-submodules", "HEAD"], timeout=10)
	except Exception:
		dirty = None

	if dirty is not None and dirty.returncode != 0:
		value += "-dirty"
	return value


def current_version() -> str:
	configured = str(os.getenv("TALOS_VERSION", "")).strip()
	if configured:
		return configured

	manifest = _manifest_version()
	if manifest:
		return manifest

	git_version = _git_local_version(_repo_root())
	if git_version:
		return git_version

	return "unknown"


def install_mode() -> str:
	if app_paths.is_frozen() and sys.platform == "win32":
		return "frozen-windows"
	if app_paths.is_frozen():
		return "frozen"
	if _has_git_checkout(_repo_root()):
		return "source-git"
	return "source"


def _normalize_version(raw: str) -> str:
	return str(raw or "").strip().lstrip("vV")


def _version_is_newer(current: str, latest: str) -> bool:
	cur = _normalize_version(current)
	lat = _normalize_version(latest)

	if not lat or lat == cur:
		return False

	try:
		from packaging.version import Version

		return Version(lat) > Version(cur)
	except Exception:
		pass

	cur_nums = [int(v) for v in re.findall(r"\d+", cur)]
	lat_nums = [int(v) for v in re.findall(r"\d+", lat)]

	if cur_nums and lat_nums:
		width = max(len(cur_nums), len(lat_nums))
		cur_pad = tuple(cur_nums + [0] * (width - len(cur_nums)))
		lat_pad = tuple(lat_nums + [0] * (width - len(lat_nums)))
		if cur_pad != lat_pad:
			return lat_pad > cur_pad

	return lat != cur


def _pick_release_from_list(releases: list[dict], channel: str) -> dict | None:
	selected_channel = _normalize_channel(channel)

	for item in releases:
		if not isinstance(item, dict):
			continue
		if bool(item.get("draft", False)):
			continue
		if selected_channel == "stable" and bool(item.get("prerelease", False)):
			continue
		return item

	return None


def _release_payload(channel: str | None = None) -> dict:
	selected_channel = _normalize_channel(channel or os.getenv("OTA_CHANNEL", "stable"))
	repo = _resolve_github_repo()
	url = _releases_api_url(repo)
	timeout_s = max(5, int(str(os.getenv("OTA_TIMEOUT_S", "15")).strip() or "15"))

	try:
		if selected_channel == "stable":
			data = _http_json(url, timeout_s=timeout_s)
		else:
			url = _releases_list_api_url(repo)
			listing = _http_json_any(url, timeout_s=timeout_s)
			if not isinstance(listing, list):
				return {
					"ok": False,
					"error": "Update server returned an invalid release list.",
					"repo": repo,
					"api_url": url,
					"channel": selected_channel,
				}
			picked = _pick_release_from_list(listing, selected_channel)
			if not picked:
				return {
					"ok": False,
					"error": "No release found for the selected update channel.",
					"repo": repo,
					"api_url": url,
					"channel": selected_channel,
				}
			data = picked
	except HTTPError as exc:
		return {
			"ok": False,
			"error": f"Update server returned HTTP {exc.code}.",
			"repo": repo,
			"api_url": url,
			"channel": selected_channel,
		}
	except URLError as exc:
		return {
			"ok": False,
			"error": f"Could not reach update server: {exc.reason}",
			"repo": repo,
			"api_url": url,
			"channel": selected_channel,
		}
	except Exception as exc:
		return {
			"ok": False,
			"error": f"Could not load update metadata: {exc}",
			"repo": repo,
			"api_url": url,
			"channel": selected_channel,
		}

	assets: list[dict] = []
	for item in data.get("assets", []):
		if not isinstance(item, dict):
			continue
		assets.append(
			{
				"name": str(item.get("name", "")).strip(),
				"url": str(item.get("browser_download_url", "")).strip(),
				"size": int(item.get("size", 0) or 0),
			}
		)

	return {
		"ok": True,
		"repo": repo,
		"channel": selected_channel,
		"api_url": url,
		"tag_name": str(data.get("tag_name", "")).strip(),
		"name": str(data.get("name", "")).strip(),
		"is_prerelease": bool(data.get("prerelease", False)),
		"published_at": str(data.get("published_at", "")).strip(),
		"html_url": str(data.get("html_url", "")).strip(),
		"body": str(data.get("body", "")).strip(),
		"assets": assets,
	}


def _release_version_label(release: dict) -> str:
	tag = str(release.get("tag_name", "")).strip()
	if tag:
		return tag
	return str(release.get("name", "")).strip()


def _can_apply(mode: str) -> tuple[bool, str]:
	if mode == "source-git":
		if not shutil.which("git"):
			return False, "Git is not installed on this machine."
		return True, "Will run git pull and refresh Python dependencies."
	if mode == "frozen-windows":
		return True, "Will download the latest Windows package and swap files automatically."
	if mode == "frozen":
		return False, "Automatic OTA apply is not supported for this packaged platform yet."
	return False, "Automatic OTA apply requires a git checkout or Windows packaged app."


def check_for_updates(channel: str | None = None) -> dict:
	enabled = _bool_env("OTA_ENABLED", True)
	selected_channel = _normalize_channel(channel or os.getenv("OTA_CHANNEL", "stable"))
	mode = install_mode()
	local_version = current_version()
	can_apply, apply_message = _can_apply(mode)

	base = {
		"ok": True,
		"enabled": enabled,
		"channel": selected_channel,
		"install_mode": mode,
		"current_version": local_version,
		"update_available": False,
		"can_apply": False,
		"apply_message": apply_message,
	}

	if not enabled:
		base.update({
			"message": "OTA updates are disabled by OTA_ENABLED=0.",
		})
		return base

	release = _release_payload(selected_channel)
	if not release.get("ok"):
		base.update(
			{
				"ok": False,
				"error": release.get("error", "Unknown update error."),
				"repo": release.get("repo", ""),
				"release_api_url": release.get("api_url", ""),
			}
		)
		return base

	latest = _release_version_label(release)
	available = _version_is_newer(local_version, latest)

	base.update(
		{
			"repo": release.get("repo", ""),
			"release_api_url": release.get("api_url", ""),
			"latest_version": latest,
			"release_name": release.get("name", ""),
			"release_is_prerelease": bool(release.get("is_prerelease", False)),
			"release_url": release.get("html_url", ""),
			"release_published_at": release.get("published_at", ""),
			"release_notes": _clip(release.get("body", ""), 2500),
			"update_available": available,
			"can_apply": available and can_apply,
			"apply_message": apply_message,
			"message": "Update available." if available else "Already on the latest version.",
		}
	)
	return base


def _pick_windows_asset(assets: list[dict]) -> dict | None:
	if not assets:
		return None

	preferred_name = str(os.getenv("OTA_WINDOWS_ASSET_NAME", DEFAULT_WINDOWS_ASSET_NAME)).strip()
	if preferred_name:
		for asset in assets:
			if asset.get("name") == preferred_name and asset.get("url"):
				return asset

	preferred_fallbacks = {
		"ClaiTALOS-windows-x64.zip",
		"ClaiTALOS-windows-x64-latest.zip",
	}
	for asset in assets:
		if asset.get("name") in preferred_fallbacks and asset.get("url"):
			return asset

	for asset in assets:
		name = str(asset.get("name", "")).lower()
		if name.endswith(".zip") and "windows" in name and asset.get("url"):
			return asset

	for asset in assets:
		name = str(asset.get("name", "")).lower()
		if name.endswith(".zip") and asset.get("url"):
			return asset

	return None


def _pick_checksum_asset(assets: list[dict]) -> dict | None:
	for asset in assets:
		name = str(asset.get("name", "")).upper()
		if name.startswith("SHA256SUMS") and name.endswith(".TXT") and asset.get("url"):
			return asset
	return None


def _write_windows_apply_script(script_path: str) -> None:
	script = r'''
param(
  [Parameter(Mandatory = $true)][string]$ZipPath,
  [Parameter(Mandatory = $true)][string]$TargetDir,
  [Parameter(Mandatory = $true)][string]$ExePath,
  [Parameter(Mandatory = $true)][int]$ProcessId
)

$ErrorActionPreference = "Stop"

$deadline = (Get-Date).AddMinutes(3)
while ($true) {
  $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
  if (-not $proc) {
	break
  }
  if ((Get-Date) -gt $deadline) {
	throw "Timed out waiting for process $ProcessId to exit."
  }
  Start-Sleep -Milliseconds 300
}

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("talos_ota_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp -Force | Out-Null

Expand-Archive -Path $ZipPath -DestinationPath $tmp -Force

$entries = Get-ChildItem -Path $tmp -Force
$sourceRoot = $tmp
if ($entries.Count -eq 1 -and $entries[0].PSIsContainer) {
  $sourceRoot = $entries[0].FullName
}

Get-ChildItem -Path $sourceRoot -Force | ForEach-Object {
  Copy-Item -Path $_.FullName -Destination $TargetDir -Recurse -Force
}

Start-Process -FilePath $ExePath -WorkingDirectory $TargetDir
'''.lstrip()

	with open(script_path, "w", encoding="utf-8") as f:
		f.write(script)


def _apply_source_git() -> dict:
	repo_root = _repo_root()
	git_bin = shutil.which("git")
	if not git_bin or not _has_git_checkout(repo_root):
		return {
			"ok": False,
			"error": "Git checkout not found; cannot apply OTA update in source mode.",
		}

	try:
		pull = _run([git_bin, "-C", repo_root, "pull", "--ff-only"], timeout=180)
	except Exception as exc:
		return {
			"ok": False,
			"error": f"git pull failed: {exc}",
		}

	if pull.returncode != 0:
		return {
			"ok": False,
			"error": "git pull failed. Resolve local changes or branch issues, then retry.",
			"details": _clip((pull.stderr or "") + "\n" + (pull.stdout or "")),
		}

	req_path = os.path.join(app_paths.source_root(), "requirements.txt")
	pip_output = ""

	if os.path.isfile(req_path):
		try:
			pip_install = _run([sys.executable, "-m", "pip", "install", "-r", req_path], timeout=900)
		except Exception as exc:
			return {
				"ok": False,
				"error": f"Dependencies update failed: {exc}",
			}

		pip_output = (pip_install.stdout or "") + "\n" + (pip_install.stderr or "")
		if pip_install.returncode != 0:
			return {
				"ok": False,
				"error": "Dependencies update failed after pulling latest code.",
				"details": _clip(pip_output),
			}

	return {
		"ok": True,
		"mode": "source-git",
		"applied": True,
		"restart_required": True,
		"message": "Update downloaded and installed. Restart TALOS to use the new version.",
		"details": {
			"git": _clip((pull.stdout or "") + "\n" + (pull.stderr or "")),
			"pip": _clip(pip_output),
		},
	}


def _apply_frozen_windows(release: dict) -> dict:
	assets = list(release.get("assets", []))
	asset = _pick_windows_asset(assets)
	if not asset:
		return {
			"ok": False,
			"error": "No compatible Windows update package found in the latest release.",
		}

	asset_name = str(asset.get("name", "") or "update.zip")
	asset_url = str(asset.get("url", "")).strip()
	if not asset_url:
		return {
			"ok": False,
			"error": "Release asset is missing a download URL.",
		}

	updates_dir = app_paths.data_path("updates")
	os.makedirs(updates_dir, exist_ok=True)

	stamp = time.strftime("%Y%m%d_%H%M%S")
	archive_name = f"{stamp}_{_safe_filename(asset_name, fallback='update.zip')}"
	archive_path = os.path.join(updates_dir, archive_name)

	timeout_s = max(5, int(str(os.getenv("OTA_TIMEOUT_S", "15")).strip() or "15"))
	try:
		_download_to_path(asset_url, archive_path, timeout_s=timeout_s)
	except Exception as exc:
		return {
			"ok": False,
			"error": f"Failed to download update package: {exc}",
		}

	checksum_asset = _pick_checksum_asset(assets)
	expected_digest = None
	if checksum_asset and checksum_asset.get("url"):
		try:
			checksum_text = _http_bytes(str(checksum_asset.get("url")), timeout_s=timeout_s).decode("utf-8", errors="replace")
			expected_digest = _find_expected_digest(checksum_text, asset_name)
		except Exception:
			expected_digest = None

	actual_digest = _sha256(archive_path)
	if expected_digest and expected_digest != actual_digest:
		return {
			"ok": False,
			"error": "Downloaded update failed checksum verification.",
			"details": f"expected={expected_digest}, actual={actual_digest}",
		}

	script_path = os.path.join(updates_dir, "apply_windows_ota.ps1")
	try:
		_write_windows_apply_script(script_path)
	except Exception as exc:
		return {
			"ok": False,
			"error": f"Could not prepare Windows updater script: {exc}",
		}

	shell = shutil.which("powershell.exe") or shutil.which("pwsh.exe") or "powershell.exe"
	creation_flags = (
		getattr(subprocess, "DETACHED_PROCESS", 0)
		| getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
		| getattr(subprocess, "CREATE_NO_WINDOW", 0)
	)

	cmd = [
		shell,
		"-NoProfile",
		"-ExecutionPolicy",
		"Bypass",
		"-File",
		script_path,
		"-ZipPath",
		archive_path,
		"-TargetDir",
		app_paths.executable_dir(),
		"-ExePath",
		sys.executable,
		"-ProcessId",
		str(os.getpid()),
	]

	try:
		subprocess.Popen(cmd, creationflags=creation_flags, close_fds=True)
	except Exception as exc:
		return {
			"ok": False,
			"error": f"Could not start updater helper process: {exc}",
		}

	return {
		"ok": True,
		"mode": "frozen-windows",
		"applied": True,
		"restarting_now": True,
		"message": "Update package staged. TALOS will now restart and complete the update.",
		"details": {
			"asset": asset_name,
			"archive_path": archive_path,
			"sha256": actual_digest,
			"checksum_verified": bool(expected_digest),
		},
	}


def apply_update(channel: str | None = None) -> dict:
	selected_channel = _normalize_channel(channel or os.getenv("OTA_CHANNEL", "stable"))
	status = check_for_updates(selected_channel)
	if not status.get("ok"):
		return status

	if not status.get("enabled", True):
		return {
			"ok": False,
			"error": "OTA updates are disabled by OTA_ENABLED=0.",
		}

	if not status.get("update_available"):
		return {
			"ok": False,
			"error": "No update is currently available.",
		}

	mode = str(status.get("install_mode", "")).strip()
	if mode == "source-git":
		return _apply_source_git()

	if mode == "frozen-windows":
		release = _release_payload(selected_channel)
		if not release.get("ok"):
			return {
				"ok": False,
				"error": release.get("error", "Could not fetch update release metadata."),
			}
		return _apply_frozen_windows(release)

	return {
		"ok": False,
		"error": "Automatic OTA apply is not supported in this runtime mode.",
	}
