"""Ollama discovery, configuration, and model-pull helpers.

Talks to Ollama's native HTTP API (``/api/tags``, ``/api/pull``, ...) rather than
shelling out to the CLI, so it works when Ollama runs as a service, in a
container, or on another host — and so model pulls can report live progress.
"""

import asyncio
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any, AsyncIterator, Callable

import httpx

logger = logging.getLogger("talos.ollama")

DEFAULT_BASE_URL = "http://localhost:11434/v1"

# Ollama unloads an idle model 5 minutes after the last request by default, so
# the next message pays the full weight-load cost again. TALOS keeps its model
# resident instead: it sends a longer keep_alive and re-arms it on a timer.
DEFAULT_KEEP_ALIVE = "30m"

# How many distinct models to hold in memory at once. Warming more than this
# thrashes RAM on the kind of machine people run Ollama on.
MAX_WARM_MODELS = 2

# Ollama loads models with a 4096-token window unless it is told otherwise, and
# TALOS spends roughly 9k of those on its system prompt and tool schemas alone.
# At the default, conversation history is silently truncated away before the
# model ever sees it -- the assistant reads as if it has amnesia. Size the window
# ourselves instead, capped by what the model actually supports.
DEFAULT_NUM_CTX = 16384

# Below this the agent cannot fit its own scaffolding, let alone a conversation.
MIN_USABLE_NUM_CTX = 12288

_model_info_cache: dict[str, dict] = {}
_ctx_logged: set[str] = set()

# Upper bound on the re-arm cycle. It has to stay well under Ollama's own
# 5-minute default TTL: the OpenAI-compatible /v1/chat/completions endpoint
# silently ignores keep_alive, so every chat resets the model's timer to that
# default no matter what we ask for. Only the native /api/generate preload below
# sets a longer TTL, so we re-issue it on this cycle.
_WARM_INTERVAL_CAP_S = 120.0
_WARM_INTERVAL_FLOOR_S = 60.0

# Loading a large model from cold disk can take minutes.
_PRELOAD_READ_TIMEOUT_S = 600.0

_DURATION_UNITS = {
    "ns": 1e-9,
    "us": 1e-6,
    "\u00b5s": 1e-6,
    "ms": 1e-3,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
}

_warm_wake = asyncio.Event()
_warm_state: dict[str, bool] = {}
_warm_loop: asyncio.AbstractEventLoop | None = None

# Curated starting points shown in the UI when nothing is installed yet.
SUGGESTED_MODELS = [
    {"name": "llama3.2:3b", "size": "2.0 GB", "note": "Small and fast. Good on 8 GB RAM."},
    {"name": "llama3.1:8b", "size": "4.7 GB", "note": "Solid all-rounder with tool calling."},
    {"name": "qwen2.5:7b", "size": "4.7 GB", "note": "Strong tool calling, good for agents."},
    {"name": "qwen2.5-coder:7b", "size": "4.7 GB", "note": "Tuned for code tasks."},
    {"name": "mistral:7b", "size": "4.1 GB", "note": "Fast general-purpose model."},
    {"name": "deepseek-r1:8b", "size": "5.2 GB", "note": "Reasoning model. Slower, more thorough."},
]


def native_base_url(base_url: str | None = None) -> str:
    """Convert the OpenAI-compatible base URL into Ollama's native API root."""
    value = (base_url or os.getenv("OLLAMA_BASE_URL", "") or DEFAULT_BASE_URL).strip()
    if not value:
        value = DEFAULT_BASE_URL
    if "://" not in value:
        value = f"http://{value}"
    value = value.rstrip("/")
    for suffix in ("/v1", "/api"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
    return value or "http://localhost:11434"


def _fmt_size(num_bytes: Any) -> str:
    try:
        size = float(num_bytes)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return ""


def cli_available() -> bool:
    return bool(shutil.which("ollama"))


def keep_alive_value() -> str:
    """The keep_alive to send with every request, honouring Settings."""
    return os.getenv("OLLAMA_KEEP_ALIVE", "").strip() or DEFAULT_KEEP_ALIVE


def keep_alive_seconds() -> float:
    """`keep_alive` in seconds. A negative duration means "never unload"."""
    raw = keep_alive_value().lower()
    if raw.startswith("-"):
        return math.inf
    try:
        return float(raw)  # a bare number is seconds, same as Ollama reads it
    except ValueError:
        pass
    total = 0.0
    for amount, unit in re.findall(r"(\d+(?:\.\d+)?)(ns|us|\u00b5s|ms|s|m|h)", raw):
        total += float(amount) * _DURATION_UNITS[unit]
    if total <= 0:
        logger.warning(f"OLLAMA_KEEP_ALIVE={raw!r} is not a duration; using {DEFAULT_KEEP_ALIVE}")
        return 1800.0
    return total


def warm_enabled() -> bool:
    """Whether TALOS should hold the model in memory between messages.

    Set OLLAMA_KEEP_WARM=0 (or a keep_alive of 0) to give the RAM back as soon
    as a reply finishes, at the cost of a reload on the next message.
    """
    raw = os.getenv("OLLAMA_KEEP_WARM", "").strip().lower()
    if raw in {"0", "false", "off", "no", "n"}:
        return False
    return keep_alive_seconds() > 0


def configured_num_ctx() -> int | None:
    """The context window set in Settings, if any."""
    raw = os.getenv("OLLAMA_NUM_CTX", "").strip()
    if not raw:
        return None
    try:
        num_ctx = int(float(raw))
    except ValueError:
        logger.warning(f"OLLAMA_NUM_CTX={raw!r} is not a number; ignoring")
        return None
    return num_ctx if num_ctx > 0 else None


async def model_info(model_name: str, base_url: str | None = None) -> dict:
    """Cached /api/show metadata for a model. Failures are not cached."""
    cached = _model_info_cache.get(model_name)
    if cached is not None:
        return cached
    info = await show_model(model_name, base_url)
    if info.get("error"):
        return {}
    _model_info_cache[model_name] = info
    return info


async def resolve_num_ctx(model_name: str, base_url: str | None = None) -> int | None:
    """The context window to run `model_name` with.

    Settings win. Otherwise ask the model how much context it supports and take
    DEFAULT_NUM_CTX or that maximum, whichever is smaller -- anything less and
    Ollama quietly truncates the conversation out of the prompt.
    """
    configured = configured_num_ctx()
    if configured:
        return configured

    model_max = int((await model_info(model_name, base_url)).get("context_length") or 0)
    if model_max <= 0:
        return None

    num_ctx = min(DEFAULT_NUM_CTX, model_max)
    if model_name not in _ctx_logged:
        _ctx_logged.add(model_name)
        if num_ctx < MIN_USABLE_NUM_CTX:
            logger.warning(
                f"{model_name} supports only {model_max} tokens of context. TALOS needs "
                f"around {MIN_USABLE_NUM_CTX} for its system prompt and tools, so earlier "
                "messages will be dropped. Consider a model with a larger context."
            )
        else:
            logger.info(f"Running {model_name} with a {num_ctx}-token context window")
    return num_ctx


async def supports_thinking(model_name: str, base_url: str | None = None) -> bool:
    """Whether the model accepts Ollama's `think` flag."""
    caps = (await model_info(model_name, base_url)).get("capabilities") or []
    return any(str(c).lower() == "thinking" for c in caps)


def forget_model_info() -> None:
    """Drop cached model metadata, e.g. after Settings changed."""
    _model_info_cache.clear()
    _ctx_logged.clear()


async def generation_options(model_name: str = "", base_url: str | None = None) -> dict[str, Any]:
    """The Ollama `options` block to send with a request.

    Every request for a model has to send the same options: changing num_ctx
    between calls makes Ollama drop the loaded model and load it again.
    """
    options: dict[str, Any] = {}
    if model_name:
        num_ctx = await resolve_num_ctx(model_name, base_url)
    else:
        num_ctx = configured_num_ctx()
    if num_ctx:
        options["num_ctx"] = num_ctx
    return options


async def get_status(base_url: str | None = None) -> dict:
    """Probe the Ollama server: reachability, version, installed and loaded models."""
    root = native_base_url(base_url)
    status: dict[str, Any] = {
        "base_url": root,
        "cli_installed": cli_available(),
        "reachable": False,
        "version": "",
        "models": [],
        "running": [],
        "suggested": SUGGESTED_MODELS,
        "error": "",
    }

    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            version_resp = await client.get(f"{root}/api/version")
            version_resp.raise_for_status()
            status["reachable"] = True
            status["version"] = str(version_resp.json().get("version", ""))

            try:
                tags = await client.get(f"{root}/api/tags")
                tags.raise_for_status()
                for entry in tags.json().get("models", []):
                    details = entry.get("details") or {}
                    status["models"].append({
                        "name": entry.get("name", ""),
                        "size": _fmt_size(entry.get("size")),
                        "size_bytes": entry.get("size", 0),
                        "parameter_size": details.get("parameter_size", ""),
                        "quantization": details.get("quantization_level", ""),
                        "family": details.get("family", ""),
                        "modified": entry.get("modified_at", ""),
                    })
                status["models"].sort(key=lambda m: m["name"])
            except Exception:
                logger.debug("Could not list Ollama models", exc_info=True)

            try:
                ps = await client.get(f"{root}/api/ps")
                ps.raise_for_status()
                for entry in ps.json().get("models", []):
                    status["running"].append({
                        "name": entry.get("name", ""),
                        "size": _fmt_size(entry.get("size")),
                        "expires": entry.get("expires_at", ""),
                    })
            except Exception:
                logger.debug("Could not list running Ollama models", exc_info=True)

    except httpx.ConnectError:
        status["error"] = (
            "Could not reach the Ollama server at "
            f"{root}. Start Ollama (run `ollama serve`) or check the base URL."
            if status["cli_installed"]
            else "Ollama is not installed. Get it from https://ollama.com, then start it."
        )
    except Exception as exc:
        status["error"] = f"Could not reach Ollama at {root}: {exc}"

    return status


async def show_model(model_name: str, base_url: str | None = None) -> dict:
    """Fetch model metadata (context length, family, quantization) from Ollama."""
    root = native_base_url(base_url)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{root}/api/show", json={"model": model_name})
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        return {"error": str(exc)}

    info = data.get("model_info") or {}
    context_length = 0
    for key, value in info.items():
        if key.endswith(".context_length"):
            try:
                context_length = int(value)
            except (TypeError, ValueError):
                pass
            break

    details = data.get("details") or {}
    return {
        "context_length": context_length,
        "parameter_size": details.get("parameter_size", ""),
        "quantization": details.get("quantization_level", ""),
        "family": details.get("family", ""),
        "capabilities": data.get("capabilities", []),
    }


async def pull_model(model_name: str, base_url: str | None = None) -> AsyncIterator[dict]:
    """Pull a model, yielding progress events as they arrive.

    Yields dicts shaped ``{"stage", "status", "percent", "completed", "total", "digest"}``
    and finishes with a ``{"stage": "done"}`` or ``{"stage": "error"}`` event.
    """
    root = native_base_url(base_url)
    payload = {"model": model_name, "stream": True}

    # Per-layer byte counters, so the overall percentage is not reset by each new layer.
    layer_completed: dict[str, int] = {}
    layer_total: dict[str, int] = {}

    try:
        timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", f"{root}/api/pull", json=payload) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    yield {"stage": "error", "error": f"Ollama returned {resp.status_code}: {body[:400]}"}
                    return

                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if event.get("error"):
                        yield {"stage": "error", "error": str(event["error"])}
                        return

                    status_text = str(event.get("status", ""))
                    digest = str(event.get("digest", ""))
                    completed = int(event.get("completed", 0) or 0)
                    total = int(event.get("total", 0) or 0)

                    if digest and total:
                        layer_total[digest] = total
                        layer_completed[digest] = completed

                    overall_total = sum(layer_total.values())
                    overall_done = sum(layer_completed.values())
                    percent = round(overall_done / overall_total * 100, 1) if overall_total else 0.0

                    yield {
                        "stage": "progress",
                        "status": status_text,
                        "percent": percent,
                        "completed": overall_done,
                        "total": overall_total,
                        "completed_label": _fmt_size(overall_done) if overall_total else "",
                        "total_label": _fmt_size(overall_total) if overall_total else "",
                        "digest": digest[:19],
                    }

                    if status_text == "success":
                        yield {"stage": "done", "percent": 100.0, "status": "success"}
                        return

        yield {"stage": "done", "percent": 100.0, "status": "success"}

    except httpx.ConnectError:
        yield {"stage": "error", "error": f"Could not reach the Ollama server at {root}. Is Ollama running?"}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Ollama pull failed")
        yield {"stage": "error", "error": str(exc)}


async def preload_model(model_name: str, base_url: str | None = None) -> bool:
    """Load a model into Ollama's memory without generating anything.

    An empty prompt makes Ollama load the weights and return straight away, and
    the keep_alive we send (re)starts the unload timer.
    """
    root = native_base_url(base_url)
    payload: dict[str, Any] = {
        "model": model_name,
        "prompt": "",
        "stream": False,
        "keep_alive": keep_alive_value(),
    }
    options = await generation_options(model_name, root)
    if options:
        payload["options"] = options

    try:
        timeout = httpx.Timeout(connect=5.0, read=_PRELOAD_READ_TIMEOUT_S, write=30.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{root}/api/generate", json=payload)
            resp.raise_for_status()
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug(f"Could not preload Ollama model {model_name}: {exc}")
        return False


def request_warm_refresh() -> None:
    """Wake the keep-warm loop so a model change is preloaded immediately.

    Safe to call from any thread — config can be saved off the event loop.
    """
    loop = _warm_loop
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if loop is None or running is loop:
        _warm_wake.set()
        return
    try:
        loop.call_soon_threadsafe(_warm_wake.set)
    except RuntimeError:
        pass


def _note_warm_result(model_name: str, warmed: bool) -> None:
    """Log only when a model's warm state flips, so the loop stays quiet."""
    if _warm_state.get(model_name) == warmed:
        return
    _warm_state[model_name] = warmed
    if warmed:
        logger.info(f"Ollama model {model_name} loaded and kept warm")
    else:
        logger.warning(f"Could not keep Ollama model {model_name} warm; is Ollama running?")


async def _wait_for_wake(stop: asyncio.Event, timeout: float) -> None:
    waiters = [asyncio.ensure_future(stop.wait()), asyncio.ensure_future(_warm_wake.wait())]
    try:
        await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()


async def keep_warm_loop(stop: asyncio.Event, models_fn: Callable[[], list[str]]) -> None:
    """Keep the configured Ollama model(s) resident for as long as TALOS runs.

    Ollama evicts an idle model once keep_alive expires, so the first message
    after a quiet stretch would otherwise wait for the weights to come back off
    disk. This preloads at startup and re-arms the timer on a shorter cycle.
    `models_fn` is re-read every pass, so switching models in Settings is picked
    up without a restart (immediately, via `request_warm_refresh`).
    """
    global _warm_loop
    _warm_loop = asyncio.get_running_loop()

    while not stop.is_set():
        _warm_wake.clear()

        try:
            models = [m for m in dict.fromkeys(models_fn() or []) if m][:MAX_WARM_MODELS]
        except Exception:
            logger.debug("Could not resolve which Ollama models to keep warm", exc_info=True)
            models = []
        if not warm_enabled():
            models = []

        for name in models:
            if stop.is_set():
                return
            _note_warm_result(name, await preload_model(name))

        for name in list(_warm_state):
            if name not in models:
                del _warm_state[name]

        keep_alive = keep_alive_seconds()
        interval = (
            _WARM_INTERVAL_CAP_S if keep_alive <= 0
            else max(_WARM_INTERVAL_FLOOR_S, min(keep_alive / 2, _WARM_INTERVAL_CAP_S))
        )
        await _wait_for_wake(stop, interval)


# ---------------------------------------------------------------------------
# Ollama self-upgrade — used by the decision layer when the installed Ollama
# is too old for /v1/systemone. Progress streams like pull_model.
# ---------------------------------------------------------------------------

OLLAMA_DARWIN_URL = "https://ollama.com/download/Ollama-darwin.zip"
OLLAMA_LINUX_INSTALL_URL = "https://ollama.com/install.sh"
# zip + extracted app + the old app kept aside during the swap.
UPGRADE_MIN_FREE_BYTES = 1_600_000_000


def _free_disk_bytes(path: str = "/") -> int:
    try:
        return shutil.disk_usage(path).free
    except Exception:
        return 0


def _run_shell(cmd: list[str], timeout: float) -> tuple[int, str]:
    """Run a one-shot command, returning (exit_code, combined output)."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {int(timeout)}s"
    except Exception as exc:
        return 1, str(exc)


def _detect_mac_install() -> str:
    """How ollama is installed on this Mac: 'app', 'homebrew', or '' (unknown)."""
    binary = shutil.which("ollama")
    if not binary:
        return ""
    try:
        real = os.path.realpath(binary)
    except Exception:
        return ""
    if "/Cellar/" in real or real.startswith("/opt/homebrew/") or real.startswith("/usr/local/Cellar/"):
        return "homebrew"
    if os.path.isdir("/Applications/Ollama.app") or "Ollama.app" in real:
        return "app"
    return ""


async def _wait_server_gone(native_root: str, timeout_s: float = 25.0) -> bool:
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient(timeout=httpx.Timeout(2.0)) as client:
        while time.monotonic() < deadline:
            try:
                await client.get(f"{native_root}/api/version")
            except Exception:
                return True
            await asyncio.sleep(1.0)
    return False


async def _wait_server_version(native_root: str, timeout_s: float = 90.0) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(f"{native_root}/api/version")
                if resp.status_code == 200:
                    return True, str(resp.json().get("version", ""))
            except Exception:
                pass
            await asyncio.sleep(2.0)
    return False, ""


async def upgrade_ollama(min_version: str = "0.40.0"):
    """Upgrade the local Ollama installation to the latest release.

    Yields progress events shaped like pull_model's:
    {"stage": "start|progress|done|error", "message": ..., "percent": 0-100}

    Supports the macOS app install (download + swap), Homebrew on macOS, and
    the official install.sh on Linux. Anything else fails loudly with an
    actionable message instead of guessing.
    """
    native_root = native_base_url()
    min_parts = tuple(int(p) for p in min_version.split("."))

    def _version_ok(version: str) -> bool:
        parts = str(version).strip().split(".")
        try:
            nums = [int(p.split("-")[0]) for p in parts[:3]]
        except (ValueError, IndexError):
            return False
        while len(nums) < 3:
            nums.append(0)
        return tuple(nums) >= min_parts

    if sys.platform == "darwin":
        method = _detect_mac_install()
        if method == "homebrew":
            yield {"stage": "progress", "message": "Upgrading Ollama via Homebrew (this can take a few minutes)..."}
            loop = asyncio.get_running_loop()
            code, output = await loop.run_in_executor(None, lambda: _run_shell(["brew", "upgrade", "ollama"], 900))
            if code != 0:
                yield {"stage": "error", "error": f"brew upgrade ollama failed: {output[-500:]}"}
                return
            yield {"stage": "progress", "message": "Restarting the Ollama service..."}
            await loop.run_in_executor(None, lambda: _run_shell(["brew", "services", "restart", "ollama"], 120))
            ok, version = await _wait_server_version(native_root)
            if not ok:
                yield {"stage": "error", "error": "Upgrade ran but the Ollama server did not come back. Start it manually and retry."}
                return
            yield {"stage": "done", "message": f"Ollama upgraded to {version}."}
            return

        if method == "app":
            free = _free_disk_bytes("/Applications")
            if free and free < UPGRADE_MIN_FREE_BYTES:
                yield {"stage": "error", "error": (
                    f"The Ollama upgrade needs about 1.6 GB of free disk space "
                    f"({free / 1_000_000_000:.1f} GB free now). Free up disk space and try again."
                )}
                return

            yield {"stage": "progress", "message": "Quitting the Ollama app..."}
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: _run_shell(["osascript", "-e", 'tell application "Ollama" to quit'], 20),
            )
            if not await _wait_server_gone(native_root):
                yield {"stage": "error", "error": "Could not stop the running Ollama app. Quit it from the menu bar and retry."}
                return

            yield {"stage": "progress", "percent": 5, "message": "Downloading the latest Ollama..."}
            import tempfile
            import zipfile

            with tempfile.TemporaryDirectory(prefix="ollama-upgrade-") as tmp:
                zip_path = os.path.join(tmp, "Ollama-darwin.zip")
                try:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0), follow_redirects=True) as client:
                        async with client.stream("GET", OLLAMA_DARWIN_URL) as resp:
                            resp.raise_for_status()
                            total = int(resp.headers.get("content-length") or 0)
                            done = 0
                            last_pct = 5.0
                            with open(zip_path, "wb") as fh:
                                async for chunk in resp.aiter_bytes(1 << 16):
                                    fh.write(chunk)
                                    done += len(chunk)
                                    if total:
                                        pct = 5.0 + 65.0 * done / total
                                        if pct - last_pct >= 10:
                                            last_pct = pct
                                            yield {"stage": "progress", "percent": round(pct), "message": f"Downloading Ollama: {round(pct)}%"}
                except Exception as exc:
                    yield {"stage": "error", "error": f"Could not download Ollama: {exc}"}
                    return

                yield {"stage": "progress", "percent": 75, "message": "Extracting..."}
                try:
                    with zipfile.ZipFile(zip_path) as zf:
                        zf.extractall(tmp)
                except Exception as exc:
                    yield {"stage": "error", "error": f"Could not extract the Ollama download: {exc}"}
                    return
                new_app = os.path.join(tmp, "Ollama.app")
                if not os.path.isdir(new_app):
                    yield {"stage": "error", "error": "The download did not contain Ollama.app."}
                    return

                yield {"stage": "progress", "percent": 85, "message": "Installing the new Ollama app..."}
                apps_dir = "/Applications"
                old_app = os.path.join(apps_dir, "Ollama.app")
                backup_app = os.path.join(apps_dir, f"Ollama.app.old-{int(time.time())}")
                swapped = False
                try:
                    if os.path.exists(old_app):
                        os.rename(old_app, backup_app)
                    shutil.move(new_app, old_app)
                    swapped = True
                except PermissionError:
                    if os.path.exists(backup_app) and not os.path.exists(old_app):
                        os.rename(backup_app, old_app)
                    yield {"stage": "error", "error": "No permission to replace /Applications/Ollama.app. Run the upgrade with an admin account."}
                    return
                except Exception as exc:
                    if os.path.exists(backup_app) and not os.path.exists(old_app):
                        os.rename(backup_app, old_app)
                    yield {"stage": "error", "error": f"Could not install the new Ollama app: {exc}"}
                    return

                yield {"stage": "progress", "percent": 92, "message": "Starting Ollama..."}
                await loop.run_in_executor(None, lambda: _run_shell(["open", "-a", "Ollama"], 30))
                ok, version = await _wait_server_version(native_root)
                if not ok or not _version_ok(version):
                    # Put the old app back rather than leave a broken install.
                    if swapped and os.path.exists(backup_app):
                        try:
                            await loop.run_in_executor(None, lambda: _run_shell(["osascript", "-e", 'tell application "Ollama" to quit'], 20))
                            await _wait_server_gone(native_root, timeout_s=10)
                            shutil.rmtree(old_app, ignore_errors=True)
                            os.rename(backup_app, old_app)
                            await loop.run_in_executor(None, lambda: _run_shell(["open", "-a", "Ollama"], 30))
                        except Exception:
                            pass
                    yield {"stage": "error", "error": (
                        f"The new Ollama did not come up correctly (version {version or 'unknown'}). "
                        "The previous version was restored — upgrade manually from https://ollama.com."
                    )}
                    return

                # Success: drop the old copy to reclaim its disk.
                if os.path.exists(backup_app):
                    shutil.rmtree(backup_app, ignore_errors=True)
                yield {"stage": "done", "message": f"Ollama upgraded to {version}."}
                return

        yield {"stage": "error", "error": (
            "Could not tell how Ollama is installed on this Mac "
            "(expected the Ollama app or Homebrew). Upgrade manually from https://ollama.com."
        )}
        return

    if sys.platform.startswith("linux"):
        # Root preflight: the official installer writes to /usr/lib and systemd.
        # Without root (or passwordless sudo) it would fail halfway through.
        is_root = hasattr(os, "geteuid") and os.geteuid() == 0
        sudo_prefix: list[str] = []
        if not is_root:
            code, _ = _run_shell(["sudo", "-n", "true"], 10)
            if code == 0:
                sudo_prefix = ["sudo"]
            else:
                yield {"stage": "error", "error": (
                    "The Ollama installer needs root. Run TALOS as root, or allow "
                    "passwordless sudo for the upgrade (sudo -n true must succeed)."
                )}
                return

        # Stream the installer's own output line by line so a slow download
        # (the Linux bundle is ~1 GB, plus GPU libraries on some servers) shows
        # live progress instead of looking stuck.
        yield {"stage": "progress", "message": "Running the official Ollama installer (downloads ~1 GB; GPU libraries are larger)..."}
        install_cmd = f"curl -fsSL {OLLAMA_LINUX_INSTALL_URL} | sh"
        if sudo_prefix:
            install_cmd = f"curl -fsSL {OLLAMA_LINUX_INSTALL_URL} | sudo sh"
        proc = await asyncio.create_subprocess_shell(
            install_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        tail: list[str] = []
        started = time.monotonic()
        wall_cap = 1800.0  # hard stop after 30 minutes of total silence-free runtime
        while True:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=wall_cap)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                yield {"stage": "error", "error": (
                    f"The Ollama installer ran for {int(wall_cap / 60)} minutes without "
                    "finishing and was stopped. Check the network to ollama.com and retry."
                )}
                return
            if not raw:
                break
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            tail.append(line)
            del tail[:-20]
            elapsed = int(time.monotonic() - started)
            yield {
                "stage": "progress",
                "message": f"{line[:180]}",
                "elapsed_s": elapsed,
            }
        try:
            code = await asyncio.wait_for(proc.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            yield {"stage": "error", "error": (
                "The Ollama installer finished its output but did not exit; it was stopped. "
                "Check `systemctl status ollama` and retry."
            )}
            return
        if code != 0:
            yield {"stage": "error", "error": (
                f"The Ollama installer failed (exit {code}). Last output: "
                + " | ".join(tail[-3:])
            )}
            return

        yield {"stage": "progress", "message": "Installer finished. Waiting for the Ollama server to come back..."}
        ok, version = await _wait_server_version(native_root)
        if not ok:
            yield {"stage": "error", "error": (
                "Installer ran but the Ollama server did not come back. "
                "Restart it (sudo systemctl restart ollama) and retry the decision model."
            )}
            return
        if not _version_ok(version):
            yield {"stage": "error", "error": f"Ollama is still version {version or 'unknown'} after the upgrade — the service may need a restart."}
            return
        yield {"stage": "done", "message": f"Ollama upgraded to {version}."}
        return

    yield {"stage": "error", "error": "Automatic Ollama upgrades are supported on macOS and Linux. On Windows, download from https://ollama.com/download/windows."}
