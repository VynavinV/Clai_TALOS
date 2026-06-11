import os
import sys
import json
import time
import asyncio
import logging
import shutil
import subprocess
import importlib
import ipaddress
import socket
from datetime import datetime, timezone
from urllib import request, error
from urllib.parse import urlparse

import app_paths

logger = logging.getLogger("talos.browser")

try:
    _playwright_async_api = importlib.import_module("playwright.async_api")
    async_playwright = _playwright_async_api.async_playwright
    PlaywrightError = _playwright_async_api.Error
    PlaywrightTimeoutError = _playwright_async_api.TimeoutError
    PLAYWRIGHT_AVAILABLE = True
    PLAYWRIGHT_IMPORT_ERROR = ""
except Exception as exc:
    async_playwright = None
    PlaywrightError = Exception
    PlaywrightTimeoutError = TimeoutError
    PLAYWRIGHT_AVAILABLE = False
    PLAYWRIGHT_IMPORT_ERROR = str(exc)

_ARTIFACT_DIR = app_paths.browser_artifacts_dir()
os.makedirs(_ARTIFACT_DIR, exist_ok=True)

_DEFAULT_ENDPOINT = os.getenv("BROWSER_CDP_ENDPOINT", "http://127.0.0.1:9222")
_DEFAULT_TIMEOUT_MS = int(os.getenv("BROWSER_DEFAULT_TIMEOUT_MS", "15000"))
_MAX_TIMEOUT_MS = 180000

_session_lock = asyncio.Lock()
_session = None
_launched_process = None


class BrowserSession:
    def __init__(self, playwright, browser, context, page, endpoint: str):
        self.playwright = playwright
        self.browser = browser
        self.context = context
        self.page = page
        self.endpoint = endpoint
        self.connected_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_int(value, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except Exception:
            return default
    if not isinstance(value, (int, float)):
        return default
    out = int(value)
    if minimum is not None:
        out = max(minimum, out)
    if maximum is not None:
        out = min(maximum, out)
    return out


def _to_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off"}:
            return False
    return default


def _normalize_endpoint(endpoint: str | None) -> str:
    if not endpoint:
        endpoint = _DEFAULT_ENDPOINT
    endpoint = endpoint.strip()
    if not endpoint.startswith("http://") and not endpoint.startswith("https://"):
        endpoint = f"http://{endpoint}"
    return endpoint.rstrip("/")


def _cdp_json_url(endpoint: str, path: str) -> str:
    return f"{endpoint}/{path.lstrip('/')}"


def _http_json(url: str, timeout_seconds: float) -> dict | list:
    req = request.Request(url, headers={"User-Agent": "Clai-TALOS/1.0"})
    with request.urlopen(req, timeout=timeout_seconds) as resp:
        data = resp.read().decode("utf-8", errors="replace")
        return json.loads(data)


async def _probe_cdp(endpoint: str) -> tuple[bool, dict]:
    version_url = _cdp_json_url(endpoint, "json/version")
    targets_url = _cdp_json_url(endpoint, "json/list")
    try:
        version = await asyncio.to_thread(_http_json, version_url, 2.0)
        targets = await asyncio.to_thread(_http_json, targets_url, 2.0)
        if not isinstance(version, dict):
            return False, {"error": "Invalid /json/version response"}
        if not isinstance(targets, list):
            targets = []
        return True, {
            "endpoint": endpoint,
            "browser": version.get("Browser", ""),
            "protocol_version": version.get("Protocol-Version", ""),
            "user_agent": version.get("User-Agent", ""),
            "websocket_url": version.get("webSocketDebuggerUrl", ""),
            "targets": len(targets),
        }
    except error.URLError as exc:
        return False, {"error": str(exc)}
    except Exception as exc:
        return False, {"error": str(exc)}


async def _wait_for_cdp(endpoint: str, timeout_seconds: int) -> tuple[bool, dict]:
    deadline = time.monotonic() + max(1, timeout_seconds)
    last_error = ""
    while time.monotonic() < deadline:
        ok, info = await _probe_cdp(endpoint)
        if ok:
            return True, info
        last_error = info.get("error", "")
        await asyncio.sleep(0.2)
    return False, {"error": last_error or "CDP endpoint did not become ready in time"}


def _default_chrome_path() -> str:
    if sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        ]
        for path in candidates:
            if os.path.isfile(path):
                return path
        return ""

    if sys.platform.startswith("linux"):
        for cmd in ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]:
            resolved = shutil.which(cmd)
            if resolved:
                return resolved
        return ""

    if sys.platform == "win32":
        candidates = []
        local_app_data = os.getenv("LOCALAPPDATA", "")
        program_files = os.getenv("PROGRAMFILES", "")
        program_files_x86 = os.getenv("PROGRAMFILES(X86)", "")
        if local_app_data:
            candidates.append(os.path.join(local_app_data, "Google", "Chrome", "Application", "chrome.exe"))
        if program_files:
            candidates.append(os.path.join(program_files, "Google", "Chrome", "Application", "chrome.exe"))
        if program_files_x86:
            candidates.append(os.path.join(program_files_x86, "Google", "Chrome", "Application", "chrome.exe"))
        for path in candidates:
            if os.path.isfile(path):
                return path
        return shutil.which("chrome") or ""

    return ""


def _default_user_data_dir() -> str:
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Google/Chrome")
    if sys.platform.startswith("linux"):
        return os.path.expanduser("~/.config/google-chrome")
    if sys.platform == "win32":
        local_app_data = os.getenv("LOCALAPPDATA", "")
        if local_app_data:
            return os.path.join(local_app_data, "Google", "Chrome", "User Data")
    return ""


def _default_isolated_profile_dir() -> str:
    configured = (os.getenv("BROWSER_ISOLATED_PROFILE_DIR", "") or "").strip()
    if configured:
        return os.path.expanduser(configured)

    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Clai_TALOS/browser-profile")
    if sys.platform.startswith("linux"):
        return os.path.expanduser("~/.local/share/clai_talos/browser-profile")
    if sys.platform == "win32":
        local_app_data = os.getenv("LOCALAPPDATA", "")
        if local_app_data:
            return os.path.join(local_app_data, "Clai_TALOS", "browser-profile")
    return os.path.join(os.path.expanduser("~"), ".clai_talos-browser-profile")


def _maybe_port_from_endpoint(endpoint: str) -> int | None:
    try:
        without_scheme = endpoint.split("://", 1)[-1]
        host_part = without_scheme.split("/", 1)[0]
        if ":" not in host_part:
            return None
        return int(host_part.rsplit(":", 1)[1])
    except Exception:
        return None


def _truncate(value: str, max_chars: int = 4000) -> str:
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + "... [truncated]"


def _artifact_path(path: str | None, suffix: str = ".png") -> str:
    if path:
        raw = path.strip()
        if os.path.isabs(raw):
            out = raw
        else:
            out = os.path.join(app_paths.data_root(), raw)
        parent = os.path.dirname(out)
        if parent:
            os.makedirs(parent, exist_ok=True)
        return out

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return os.path.join(_ARTIFACT_DIR, f"shot_{stamp}{suffix}")


async def _session_tabs(session: BrowserSession, limit: int = 20) -> tuple[list[dict], int]:
    tabs = []
    pages = list(session.context.pages)
    active_index = -1

    for idx, candidate in enumerate(pages[: max(1, limit)]):
        if candidate == session.page:
            active_index = idx
        try:
            title = await candidate.title()
        except Exception:
            title = ""
        tabs.append(
            {
                "index": idx,
                "url": candidate.url,
                "title": title,
            }
        )

    return tabs, active_index


def _playwright_not_available_error() -> dict:
    if PLAYWRIGHT_AVAILABLE:
        return {}

    if sys.platform == "win32":
        install_cmd = ".\\venv\\Scripts\\pip install playwright"
    else:
        install_cmd = "./venv/bin/pip install playwright"

    return {
        "error": f"Playwright is not installed. Install with: {install_cmd}",
        "detail": PLAYWRIGHT_IMPORT_ERROR,
        "hint": "You can connect to your existing logged-in Chrome after installing Playwright.",
    }


def _runtime_connect_defaults() -> dict:
    endpoint = _normalize_endpoint(os.getenv("BROWSER_CDP_ENDPOINT", _DEFAULT_ENDPOINT))
    port = _maybe_port_from_endpoint(endpoint) or 9222
    return {
        "endpoint": endpoint,
        "port": port,
        "start_if_needed": _to_bool(os.getenv("BROWSER_START_IF_NEEDED", "1"), True),
        "allow_isolated_fallback": _to_bool(os.getenv("BROWSER_ALLOW_ISOLATED_FALLBACK", "0"), False),
        "isolated_profile_dir": (os.getenv("BROWSER_ISOLATED_PROFILE_DIR", "") or "").strip(),
        "chrome_path": (os.getenv("BROWSER_CHROME_PATH", "") or "").strip(),
        "user_data_dir": (os.getenv("BROWSER_CHROME_USER_DATA_DIR", "") or "").strip(),
        "profile_directory": (os.getenv("BROWSER_PROFILE_DIRECTORY", "Default") or "").strip(),
        "startup_timeout_s": _to_int(os.getenv("BROWSER_STARTUP_TIMEOUT_S", "20"), 20, 1, 120),
    }


async def _close_session_locked(terminate_launched: bool = False) -> None:
    global _session, _launched_process

    if _session is not None:
        try:
            await _session.browser.close()
        except Exception:
            pass
        try:
            await _session.playwright.stop()
        except Exception:
            pass
        _session = None

    if terminate_launched and _launched_process is not None:
        try:
            if _launched_process.poll() is None:
                _launched_process.terminate()
                try:
                    await asyncio.to_thread(_launched_process.wait, 5)
                except Exception:
                    _launched_process.kill()
        except Exception:
            pass
        _launched_process = None


async def start_chrome_debug(
    endpoint: str = _DEFAULT_ENDPOINT,
    port: int = 9222,
    chrome_path: str = "",
    user_data_dir: str = "",
    profile_directory: str = "",
    connect: bool = True,
    startup_timeout_s: int = 20,
) -> dict:
    endpoint = _normalize_endpoint(endpoint)
    probed, probe_info = await _probe_cdp(endpoint)
    if probed:
        result = {
            "status": "already_running",
            "endpoint": endpoint,
            "cdp": probe_info,
        }
        if connect:
            result["connection"] = await connect_browser(endpoint=endpoint)
        return result

    chrome_binary = chrome_path.strip() if chrome_path else _default_chrome_path()
    if not chrome_binary or not os.path.exists(chrome_binary):
        return {
            "error": "Chrome executable not found",
            "hint": "Provide chrome_path or install Google Chrome.",
        }

    if not user_data_dir:
        user_data_dir = _default_user_data_dir()

    effective_port = _to_int(port, 9222, 1, 65535)
    endpoint_port = _maybe_port_from_endpoint(endpoint)
    if endpoint_port:
        effective_port = endpoint_port

    cmd = [
        chrome_binary,
        f"--remote-debugging-port={effective_port}",
        "--no-first-run",
        "--no-default-browser-check",
    ]

    if user_data_dir:
        cmd.append(f"--user-data-dir={user_data_dir}")
    if profile_directory:
        cmd.append(f"--profile-directory={profile_directory}")
    # Do not force a startup URL. Passing about:blank can create noisy extra tabs
    # when Chrome is already running and command-line launch gets delegated.

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        return {"error": f"Failed to launch Chrome: {exc}"}

    final_endpoint = _normalize_endpoint(f"http://127.0.0.1:{effective_port}")
    ready, ready_info = await _wait_for_cdp(final_endpoint, _to_int(startup_timeout_s, 20, 1, 120))
    if not ready:
        crashed = proc.poll() is not None
        return {
            "error": "Chrome debug endpoint did not become ready",
            "endpoint": final_endpoint,
            "process_exited": crashed,
            "hint": (
                "If you are reusing your normal Chrome profile, close all running Chrome windows first, "
                "then retry with the same user_data_dir/profile_directory."
            ),
            "detail": ready_info.get("error", ""),
        }

    async with _session_lock:
        global _launched_process
        _launched_process = proc

    result = {
        "status": "started",
        "endpoint": final_endpoint,
        "chrome_path": chrome_binary,
        "user_data_dir": user_data_dir,
        "profile_directory": profile_directory,
        "pid": proc.pid,
    }

    if connect:
        result["connection"] = await connect_browser(endpoint=final_endpoint)
    return result


async def connect_browser(
    endpoint: str = _DEFAULT_ENDPOINT,
    start_if_needed: bool = False,
    allow_isolated_fallback: bool = True,
    isolated_profile_dir: str = "",
    port: int = 9222,
    chrome_path: str = "",
    user_data_dir: str = "",
    profile_directory: str = "",
    startup_timeout_s: int = 20,
) -> dict:
    dependency_error = _playwright_not_available_error()
    if dependency_error:
        return dependency_error

    endpoint = _normalize_endpoint(endpoint)

    ok, probe = await _probe_cdp(endpoint)
    if not ok and start_if_needed:
        boot = await start_chrome_debug(
            endpoint=endpoint,
            port=port,
            chrome_path=chrome_path,
            user_data_dir=user_data_dir,
            profile_directory=profile_directory,
            connect=False,
            startup_timeout_s=startup_timeout_s,
        )
        if "error" in boot:
            if allow_isolated_fallback and boot.get("error") == "Chrome debug endpoint did not become ready":
                fallback_profile = os.path.expanduser((isolated_profile_dir or "").strip() or _default_isolated_profile_dir())
                os.makedirs(fallback_profile, exist_ok=True)
                fallback_boot = await start_chrome_debug(
                    endpoint=endpoint,
                    port=port,
                    chrome_path=chrome_path,
                    user_data_dir=fallback_profile,
                    profile_directory="",
                    connect=False,
                    startup_timeout_s=startup_timeout_s,
                )
                if "error" in fallback_boot:
                    return {
                        "error": "Failed to start browser in both profile and isolated fallback",
                        "primary": boot,
                        "fallback": fallback_boot,
                    }
                endpoint = _normalize_endpoint(fallback_boot.get("endpoint") or endpoint)
                ok, probe = await _probe_cdp(endpoint)
            else:
                return boot
        else:
            endpoint = _normalize_endpoint(boot.get("endpoint") or endpoint)
            ok, probe = await _probe_cdp(endpoint)

    if not ok:
        return {
            "error": "Chrome CDP endpoint is not reachable",
            "endpoint": endpoint,
            "hint": (
                "Start Chrome with --remote-debugging-port=9222 and your logged-in profile, "
                "or call browser_start_chrome_debug first."
            ),
            "detail": probe.get("error", ""),
        }

    async with _session_lock:
        global _session

        if _session is not None and _session.endpoint == endpoint:
            tabs, active_index = await _session_tabs(_session)
            return {
                "status": "already_connected",
                "endpoint": endpoint,
                "connected_at": _session.connected_at,
                "tabs": tabs,
                "active_page_index": active_index,
                "current_url": _session.page.url,
                "cdp": probe,
            }

        if _session is not None:
            await _close_session_locked(terminate_launched=False)

        playwright = await async_playwright().start()
        browser = await playwright.chromium.connect_over_cdp(endpoint)

        contexts = list(browser.contexts)
        if contexts:
            context = max(contexts, key=lambda c: len(c.pages))
        else:
            context = await browser.new_context()

        pages = list(context.pages)
        page = None
        for candidate in reversed(pages):
            url = candidate.url or ""
            if not url.startswith("chrome-extension://"):
                page = candidate
                break
        if page is None:
            page = pages[-1] if pages else await context.new_page()

        page.set_default_timeout(_DEFAULT_TIMEOUT_MS)
        _session = BrowserSession(playwright, browser, context, page, endpoint)

        tabs, active_index = await _session_tabs(_session)
        return {
            "status": "connected",
            "endpoint": endpoint,
            "connected_at": _session.connected_at,
            "tabs": tabs,
            "active_page_index": active_index,
            "current_url": _session.page.url,
            "cdp": probe,
        }


async def get_state(
    include_tabs: bool = True,
    include_page_text: bool = False,
    text_limit: int = 1200,
) -> dict:
    async with _session_lock:
        if _session is None:
            return {"error": "No browser session. Call browser_connect first."}

        session = _session
        try:
            title = await session.page.title()
        except Exception:
            title = ""

        result = {
            "connected": True,
            "endpoint": session.endpoint,
            "connected_at": session.connected_at,
            "current_url": session.page.url,
            "title": title,
        }

        if include_tabs:
            tabs, active_index = await _session_tabs(session)
            result["tabs"] = tabs
            result["active_page_index"] = active_index

        if include_page_text:
            try:
                body_text = await session.page.locator("body").inner_text()
                result["page_text"] = _truncate(body_text, _to_int(text_limit, 1200, 200, 10000))
            except Exception as exc:
                result["page_text_error"] = str(exc)

        return result


async def disconnect_browser(terminate_launched: bool = False) -> dict:
    async with _session_lock:
        had_session = _session is not None
        await _close_session_locked(terminate_launched=terminate_launched)
        return {
            "ok": True,
            "disconnected": had_session,
            "terminated_launched_chrome": bool(terminate_launched),
        }


async def _wait_url_contains(page, needle: str, timeout_ms: int) -> None:
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        if needle in (page.url or ""):
            return
        await asyncio.sleep(0.1)
    raise TimeoutError(f"Timed out waiting for URL to contain: {needle}")


async def _execute_step(session: BrowserSession, page, step: dict, default_timeout_ms: int):
    action = str(step.get("action", "")).strip().lower()
    if not action:
        raise ValueError("Step is missing action")

    timeout_ms = _to_int(step.get("timeout_ms", default_timeout_ms), default_timeout_ms, 100, _MAX_TIMEOUT_MS)

    if action == "set_page":
        page_index = _to_int(step.get("page_index", 0), 0, 0, 1000)
        pages = list(session.context.pages)
        if page_index >= len(pages):
            raise ValueError(f"Invalid page_index {page_index}. Available pages: {len(pages)}")
        session.page = pages[page_index]
        return session.page, {"page_index": page_index, "url": session.page.url}

    if action == "new_tab":
        url = str(step.get("url", "about:blank")).strip() or "about:blank"
        page = await session.context.new_page()
        session.page = page
        if url and url != "about:blank":
            await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        return page, {"url": page.url}

    if action == "close_tab":
        pages_before = list(session.context.pages)
        if len(pages_before) <= 1:
            raise ValueError("Cannot close the last tab")
        await page.close()
        session.page = session.context.pages[0]
        return session.page, {"remaining_tabs": len(session.context.pages), "current_url": session.page.url}

    if action == "goto":
        url = str(step.get("url", "")).strip()
        if not url:
            raise ValueError("goto requires url")
        parsed = urlparse(url)
        if parsed.scheme.lower() not in ("http", "https"):
            raise ValueError("goto only supports http/https URLs")
        hostname = parsed.hostname or ""
        if hostname.endswith(".local") or hostname.endswith(".internal"):
            raise ValueError("Access to local/internal hostnames is blocked")
        try:
            addrs = socket.getaddrinfo(hostname, None)
            for family, _, _, _, sockaddr in addrs:
                ip = ipaddress.ip_address(sockaddr[0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    raise ValueError("Access to private/internal IP addresses is blocked")
        except (socket.gaierror, ValueError) as e:
            if isinstance(e, ValueError):
                raise
        wait_until = str(step.get("wait_until", "domcontentloaded")).strip() or "domcontentloaded"
        response = await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        status = response.status if response is not None else None
        return page, {"url": page.url, "status": status}

    if action == "click":
        selector = str(step.get("selector", "")).strip()
        if not selector:
            raise ValueError("click requires selector")
        button = str(step.get("button", "left")).strip() or "left"
        click_count = _to_int(step.get("click_count", 1), 1, 1, 5)
        strict_visibility = _to_bool(step.get("strict_visibility", False), False)
        try:
            await page.click(selector, timeout=timeout_ms, button=button, click_count=click_count)
            return page, {"clicked": selector}
        except PlaywrightTimeoutError:
            if strict_visibility:
                raise
            await page.eval_on_selector(
                selector,
                "(el) => { el.scrollIntoView({block: 'center', inline: 'center'}); el.click(); }",
            )
            return page, {"clicked": selector, "fallback": "dom_click"}

    if action == "type":
        selector = str(step.get("selector", "")).strip()
        text = str(step.get("text", ""))
        if not selector:
            raise ValueError("type requires selector")
        clear = _to_bool(step.get("clear", True), True)
        delay_ms = _to_int(step.get("delay_ms", 0), 0, 0, 500)
        strict_visibility = _to_bool(step.get("strict_visibility", False), False)
        try:
            if clear:
                await page.fill(selector, "", timeout=timeout_ms)
            if delay_ms > 0:
                await page.click(selector, timeout=timeout_ms)
                await page.type(selector, text, delay=delay_ms, timeout=timeout_ms)
            else:
                await page.fill(selector, text, timeout=timeout_ms)
            return page, {"typed": selector, "chars": len(text)}
        except PlaywrightTimeoutError:
            if strict_visibility:
                raise
            await page.eval_on_selector(
                selector,
                """
                (el, payload) => {
                    el.scrollIntoView({block: 'center', inline: 'center'});
                    if (payload.clear) {
                        el.value = '';
                    }
                    el.value = payload.text;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }
                """,
                {"text": text, "clear": bool(clear)},
            )
            return page, {"typed": selector, "chars": len(text), "fallback": "dom_value_set"}

    if action == "press":
        key = str(step.get("key", "")).strip()
        if not key:
            raise ValueError("press requires key")
        selector = str(step.get("selector", "")).strip()
        if selector:
            await page.press(selector, key, timeout=timeout_ms)
        else:
            await page.keyboard.press(key)
        return page, {"key": key, "selector": selector or None}

    if action == "wait_for":
        selector = str(step.get("selector", "")).strip()
        text = str(step.get("text", "")).strip()
        url_contains = str(step.get("url_contains", "")).strip()
        milliseconds = _to_int(step.get("milliseconds", 0), 0, 0, 600000)

        if selector:
            state = str(step.get("state", "visible")).strip() or "visible"
            retries = _to_int(step.get("retries", 2), 2, 1, 6)
            retry_backoff_ms = _to_int(step.get("retry_backoff_ms", 350), 350, 50, 5000)
            state_explicit = "state" in step
            locator = page.locator(selector).first

            for attempt in range(1, retries + 1):
                try:
                    await locator.wait_for(state=state, timeout=timeout_ms)
                    return page, {
                        "waited_for": "selector",
                        "selector": selector,
                        "state": state,
                        "attempt": attempt,
                    }
                except PlaywrightTimeoutError:
                    # For default waits, accept an attached node as a pragmatic fallback.
                    if state == "visible" and not state_explicit:
                        try:
                            await locator.wait_for(state="attached", timeout=min(timeout_ms, 2500))
                            return page, {
                                "waited_for": "selector",
                                "selector": selector,
                                "state": "attached",
                                "fallback": "visible_timeout_attached_success",
                                "attempt": attempt,
                            }
                        except Exception:
                            pass

                    if attempt < retries:
                        try:
                            await page.wait_for_load_state("domcontentloaded", timeout=min(timeout_ms, 3000))
                        except Exception:
                            pass
                        await asyncio.sleep((retry_backoff_ms * attempt) / 1000.0)
                        continue
                    raise

        if text:
            await page.get_by_text(text).first.wait_for(timeout=timeout_ms)
            return page, {"waited_for": "text", "text": text}

        if url_contains:
            await _wait_url_contains(page, url_contains, timeout_ms)
            return page, {"waited_for": "url_contains", "url_contains": url_contains}

        if milliseconds > 0:
            await asyncio.sleep(milliseconds / 1000.0)
            return page, {"waited_for": "milliseconds", "milliseconds": milliseconds}

        raise ValueError("wait_for requires selector, text, url_contains, or milliseconds")

    if action == "extract":
        selector = str(step.get("selector", "body")).strip() or "body"
        attr = str(step.get("attr", "")).strip()
        extract_all = _to_bool(step.get("all", False), False)
        limit = _to_int(step.get("limit", 5), 5, 1, 200)

        if extract_all:
            locator = page.locator(selector)
            count = await locator.count()
            out = []
            for idx in range(min(count, limit)):
                node = locator.nth(idx)
                if attr:
                    value = await node.get_attribute(attr)
                    out.append(value or "")
                else:
                    value = await node.inner_text()
                    out.append(_truncate(value, 4000))
            return page, {"selector": selector, "count": len(out), "items": out, "attr": attr or None}

        locator = page.locator(selector).first
        if attr:
            value = await locator.get_attribute(attr)
            return page, {"selector": selector, "attr": attr, "value": value}
        value = await locator.inner_text()
        return page, {"selector": selector, "value": _truncate(value, 8000)}

    if action == "select":
        selector = str(step.get("selector", "")).strip()
        if not selector:
            raise ValueError("select requires selector")
        value = step.get("value")
        label = step.get("label")
        index = step.get("index")
        strict_visibility = _to_bool(step.get("strict_visibility", False), False)
        try:
            if value is not None:
                selected = await page.select_option(selector, value=str(value), timeout=timeout_ms)
            elif label is not None:
                selected = await page.select_option(selector, label=str(label), timeout=timeout_ms)
            elif isinstance(index, (int, float)):
                selected = await page.select_option(selector, index=int(index), timeout=timeout_ms)
            else:
                raise ValueError("select requires one of value, label, or index")
            return page, {"selector": selector, "selected": selected}
        except PlaywrightTimeoutError:
            if strict_visibility:
                raise
            payload = {
                "value": str(value) if value is not None else None,
                "label": str(label) if label is not None else None,
                "index": int(index) if isinstance(index, (int, float)) else None,
            }
            selected_value = await page.eval_on_selector(
                selector,
                """
                (el, p) => {
                    el.scrollIntoView({block: 'center', inline: 'center'});
                    let option = null;
                    if (p.value !== null) {
                        option = Array.from(el.options).find(o => o.value === p.value);
                    } else if (p.label !== null) {
                        option = Array.from(el.options).find(o => o.label === p.label || o.text === p.label);
                    } else if (p.index !== null && p.index >= 0 && p.index < el.options.length) {
                        option = el.options[p.index];
                    }
                    if (!option) {
                        throw new Error('No matching option found for fallback select');
                    }
                    el.value = option.value;
                    option.selected = true;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    return option.value;
                }
                """,
                payload,
            )
            return page, {"selector": selector, "selected": [selected_value], "fallback": "dom_select"}

    if action == "scroll":
        selector = str(step.get("selector", "")).strip()
        if selector:
            await page.locator(selector).first.scroll_into_view_if_needed(timeout=timeout_ms)
            return page, {"scrolled_to": selector}
        x = float(step.get("x", 0))
        y = float(step.get("y", 600))
        await page.mouse.wheel(x, y)
        return page, {"wheel": {"x": x, "y": y}}

    if action == "screenshot":
        path = _artifact_path(step.get("path"), suffix=".png")
        full_page = _to_bool(step.get("full_page", False), False)
        await page.screenshot(path=path, full_page=full_page)
        return page, {"path": path, "full_page": full_page}

    if action == "assert":
        selector = str(step.get("selector", "")).strip()
        text = str(step.get("text", "")).strip()
        url_contains = str(step.get("url_contains", "")).strip()

        if selector:
            await page.wait_for_selector(selector, state="visible", timeout=timeout_ms)
        if text:
            body_text = await page.locator("body").inner_text()
            if text not in body_text:
                raise ValueError(f"Expected text not found: {text}")
        if url_contains and url_contains not in (page.url or ""):
            raise ValueError(f"Expected URL to contain: {url_contains}. Current URL: {page.url}")

        if not selector and not text and not url_contains:
            raise ValueError("assert requires selector, text, or url_contains")

        return page, {
            "asserted": {
                "selector": selector or None,
                "text": text or None,
                "url_contains": url_contains or None,
            }
        }

    raise ValueError(f"Unknown action: {action}")


async def run_steps(
    steps: list[dict],
    stop_on_error: bool = True,
    default_timeout_ms: int = _DEFAULT_TIMEOUT_MS,
    page_index: int | None = None,
    auto_connect: bool | None = None,
) -> dict:
    if not isinstance(steps, list) or not steps:
        return {"error": "steps must be a non-empty list"}

    dependency_error = _playwright_not_available_error()
    if dependency_error:
        return dependency_error

    if auto_connect is None:
        auto_connect = _to_bool(os.getenv("BROWSER_AUTO_CONNECT_ON_RUN", "1"), True)

    if _session is None and auto_connect:
        cfg = _runtime_connect_defaults()
        connect_result = await connect_browser(
            endpoint=cfg["endpoint"],
            start_if_needed=cfg["start_if_needed"],
            allow_isolated_fallback=cfg["allow_isolated_fallback"],
            isolated_profile_dir=cfg["isolated_profile_dir"],
            port=cfg["port"],
            chrome_path=cfg["chrome_path"],
            user_data_dir=cfg["user_data_dir"],
            profile_directory=cfg["profile_directory"],
            startup_timeout_s=cfg["startup_timeout_s"],
        )
        if "error" in connect_result:
            return {
                "error": "No browser session. Automatic connect/start failed.",
                "connect_attempt": connect_result,
            }

    async with _session_lock:
        if _session is None:
            return {"error": "No browser session. Call browser_connect first."}

        session = _session
        page = session.page

        if isinstance(page_index, (int, float)):
            idx = int(page_index)
            pages = list(session.context.pages)
            if idx < 0 or idx >= len(pages):
                return {"error": f"Invalid page_index {idx}. Available pages: {len(pages)}"}
            page = pages[idx]
            session.page = page

        default_timeout_ms = _to_int(default_timeout_ms, _DEFAULT_TIMEOUT_MS, 100, _MAX_TIMEOUT_MS)

        results = []
        completed = 0
        stopped_on_error = False

        for i, raw_step in enumerate(steps, start=1):
            if not isinstance(raw_step, dict):
                entry = {
                    "step": i,
                    "ok": False,
                    "error": "Each step must be an object",
                    "action": None,
                }
                results.append(entry)
                if stop_on_error:
                    stopped_on_error = True
                    break
                continue

            action = str(raw_step.get("action", "")).strip().lower() or None

            try:
                page, step_result = await _execute_step(session, page, raw_step, default_timeout_ms)
                completed += 1
                results.append(
                    {
                        "step": i,
                        "ok": True,
                        "action": action,
                        "result": step_result,
                    }
                )
            except PlaywrightTimeoutError as exc:
                results.append(
                    {
                        "step": i,
                        "ok": False,
                        "action": action,
                        "error": f"Timeout: {exc}",
                    }
                )
                if stop_on_error:
                    stopped_on_error = True
                    break
            except (PlaywrightError, TimeoutError, ValueError) as exc:
                results.append(
                    {
                        "step": i,
                        "ok": False,
                        "action": action,
                        "error": str(exc),
                    }
                )
                if stop_on_error:
                    stopped_on_error = True
                    break
            except Exception as exc:
                logger.exception("browser step failed")
                results.append(
                    {
                        "step": i,
                        "ok": False,
                        "action": action,
                        "error": str(exc),
                    }
                )
                if stop_on_error:
                    stopped_on_error = True
                    break

        session.page = page

        tabs, active_index = await _session_tabs(session)
        all_ok = all(r.get("ok") for r in results) if results else False

        return {
            "ok": all_ok,
            "endpoint": session.endpoint,
            "steps_requested": len(steps),
            "steps_completed": completed,
            "stopped_on_error": stopped_on_error,
            "current_url": page.url,
            "active_page_index": active_index,
            "tabs": tabs,
            "results": results,
        }
