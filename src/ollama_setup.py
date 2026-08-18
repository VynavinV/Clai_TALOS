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


def generation_options() -> dict[str, Any]:
    """The Ollama `options` block configured in Settings.

    Every request has to send the same options: changing num_ctx between calls
    makes Ollama drop the loaded model and load it again.
    """
    options: dict[str, Any] = {}
    raw = os.getenv("OLLAMA_NUM_CTX", "").strip()
    if raw:
        try:
            num_ctx = int(float(raw))
        except ValueError:
            logger.warning(f"OLLAMA_NUM_CTX={raw!r} is not a number; ignoring")
            num_ctx = 0
        if num_ctx > 0:
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
    options = generation_options()
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
