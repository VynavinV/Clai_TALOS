"""Ollama discovery, configuration, and model-pull helpers.

Talks to Ollama's native HTTP API (``/api/tags``, ``/api/pull``, ...) rather than
shelling out to the CLI, so it works when Ollama runs as a service, in a
container, or on another host — and so model pulls can report live progress.
"""

import asyncio
import json
import logging
import os
import shutil
from typing import Any, AsyncIterator

import httpx

logger = logging.getLogger("talos.ollama")

DEFAULT_BASE_URL = "http://localhost:11434/v1"

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
