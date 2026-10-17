"""Laya / Jev decision layer for Clai TALOS.

A "System One" decision model answers typed questions (choice, score, noul)
about a state in one non-autoregressive pass and returns calibrated
probabilities instead of generated text — nothing to parse, nothing to
hallucinate. Two interchangeable backends speak the same /v1/systemone wire
protocol:

- Laya (default): Convai Innovations' open-source Apache-2.0 model, served
  locally through Ollama 0.40+ (``ollama pull laya``) or any Jev-compatible
  endpoint such as ``laya-serve``.
- Jev: TypeSafe AI's hosted model, configured with ``JEV_API_KEY``.

Everything in TALOS that consumes this layer must treat it as advisory:
on any error or missing backend the answer is ``None`` and callers fall back
to their existing behavior (fail-open). One loud, throttled warning per
feature says what is wrong without spamming the log.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

import app_paths
import ollama_setup
from dotenv import load_dotenv

load_dotenv(dotenv_path=app_paths.env_file_path())

logger = logging.getLogger("talos.laya")

# Decision audit trail (JSON lines). State content is deliberately NOT logged,
# only question ids, answer values, and latency — an email body being triaged
# should not end up in a log file.
decision_logger = logging.getLogger("talos.laya.decisions")
decision_logger.setLevel(logging.INFO)
try:
    _decision_handler = logging.FileHandler(os.path.join(app_paths.logs_dir(), "laya_decisions.log"))
    _decision_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    decision_logger.addHandler(_decision_handler)
    decision_logger.propagate = False
except Exception:
    pass

QUESTION_TYPES = {"choice", "score", "noul"}
MAX_QUESTIONS_PER_CALL = 50
MAX_OPTIONS_PER_CHOICE = 255
# Ollama caps request bodies at 64 KiB; leave room for the questions payload.
MAX_STATE_CHARS = 48_000
# The /v1/systemone endpoint shipped in Ollama 0.40.
MIN_OLLAMA_VERSION = (0, 40, 0)
AVAILABILITY_TTL_S = 300.0
WARN_INTERVAL_S = 600.0

DEFAULT_JEV_BASE_URL = "https://api.typesafe.ai/v1"

FEATURE_ENV_KEYS = {
    "email_triage": "LAYA_EMAIL_TRIAGE",
    "memory_rerank": "LAYA_MEMORY_RERANK",
    "context_compact": "LAYA_CONTEXT_COMPACT",
    "model_triage": "LAYA_MODEL_TRIAGE",
    "browser_auto": "LAYA_BROWSER_AUTO",
}


class LayaError(Exception):
    """A decision-layer failure the caller can show or log."""


class LayaUnavailableError(LayaError):
    """No usable decision backend is configured or reachable."""


# ---------------------------------------------------------------------------
# Configuration (read live so Settings changes apply without a restart)
# ---------------------------------------------------------------------------


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def enabled() -> bool:
    return _env("LAYA_ENABLED", "1") != "0"


def laya_model() -> str:
    return _env("LAYA_MODEL", "laya") or "laya"


def timeout_s() -> float:
    try:
        return max(1.0, float(_env("LAYA_TIMEOUT_S", "5")))
    except ValueError:
        return 5.0


def confidence_min() -> float:
    try:
        return min(1.0, max(0.0, float(_env("LAYA_CONFIDENCE_MIN", "0.75"))))
    except ValueError:
        return 0.75


def auto_pull_enabled() -> bool:
    return _env("LAYA_AUTO_PULL", "1") != "0"


def feature_enabled(feature: str) -> bool:
    if not enabled():
        return False
    key = FEATURE_ENV_KEYS.get(feature)
    if not key:
        return False
    return os.getenv(key, "1") != "0"


# ---------------------------------------------------------------------------
# Question builders
# ---------------------------------------------------------------------------


def choice(instructions: str, options: dict) -> dict:
    """A question whose answer is one of `options` (id -> description)."""
    return {"type": "choice", "instructions": instructions, "criteria": options}


def score(instructions: str, levels: list) -> dict:
    """A question whose answer is a position along the ordered `levels`."""
    return {"type": "score", "instructions": instructions, "criteria": levels}


def noul(instructions: str) -> dict:
    """A yes/no question answered with a probability in [0, 1]."""
    return {"type": "noul", "instructions": instructions}


def normalize_questions(questions: Any) -> dict:
    """Validate and normalize a question map before it goes on the wire."""
    if not isinstance(questions, dict) or not questions:
        raise LayaError("questions must be a non-empty object mapping ids to typed questions.")
    if len(questions) > MAX_QUESTIONS_PER_CALL:
        raise LayaError(f"At most {MAX_QUESTIONS_PER_CALL} questions per call, got {len(questions)}.")

    normalized: dict[str, dict] = {}
    for raw_id, spec in questions.items():
        qid = str(raw_id).strip()
        if not qid:
            raise LayaError("Question ids must be non-empty strings.")
        if not isinstance(spec, dict):
            raise LayaError(f"Question {qid!r} must be an object with type, instructions and criteria.")

        qtype = str(spec.get("type", "")).strip().lower()
        if qtype not in QUESTION_TYPES:
            raise LayaError(f"Question {qid!r} has invalid type {qtype!r}; use one of {sorted(QUESTION_TYPES)}.")

        instructions = str(spec.get("instructions", "")).strip()
        if not instructions:
            raise LayaError(f"Question {qid!r} needs instructions: the question text to judge against the state.")

        out = {"type": qtype, "instructions": instructions}
        criteria = spec.get("criteria")

        if qtype == "choice":
            if not isinstance(criteria, dict) or not criteria:
                raise LayaError(f"Choice question {qid!r} needs criteria: an object of option id -> description.")
            options: dict[str, str] = {}
            for key, desc in criteria.items():
                opt_id = str(key).strip()
                if not opt_id:
                    raise LayaError(f"Choice question {qid!r} has an empty option id.")
                options[opt_id] = str(desc or "")
            if len(options) > MAX_OPTIONS_PER_CHOICE:
                raise LayaError(f"Choice question {qid!r} has {len(options)} options; the cap is {MAX_OPTIONS_PER_CHOICE}.")
            out["criteria"] = options
        elif qtype == "score":
            if not isinstance(criteria, (list, tuple)) or len(criteria) < 2:
                raise LayaError(f"Score question {qid!r} needs criteria: an ordered list of 2+ level descriptions.")
            out["criteria"] = [str(level) for level in criteria]
        else:
            if criteria is not None:
                out["criteria"] = str(criteria)

        normalized[qid] = out
    return normalized


# ---------------------------------------------------------------------------
# Provider resolution
# ---------------------------------------------------------------------------


def resolve_provider() -> str:
    """Which backend the user prefers: ollama, jev, custom, local, or auto."""
    pref = _env("LAYA_PROVIDER", "auto").lower()
    if pref in {"ollama", "jev", "custom", "local"}:
        return pref
    # Auto: an explicit endpoint is the most specific, then a hosted key,
    # then the local Ollama, and finally the built-in package when Ollama
    # is too old for /v1/systemone.
    if _env("LAYA_BASE_URL"):
        return "custom"
    if _env("JEV_API_KEY"):
        return "jev"
    return "auto"


def _systemone_url(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if "://" not in base:
        base = f"http://{base}"
    if base.endswith("/v1"):
        return f"{base}/systemone"
    return f"{base}/v1/systemone"


def provider_endpoint(provider: str | None = None) -> dict:
    """Resolve the active backend: URL, headers, and model to pin."""
    provider = provider or resolve_provider()

    headers = {"Content-Type": "application/json"}
    api_key = _env("JEV_API_KEY")

    if provider == "jev":
        base = _env("LAYA_BASE_URL") or _env("JEV_BASE_URL", DEFAULT_JEV_BASE_URL)
        model = _env("LAYA_JEV_MODEL", "jev-1") or None
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
    elif provider == "custom":
        base = _env("LAYA_BASE_URL") or _env("JEV_BASE_URL", DEFAULT_JEV_BASE_URL)
        model = laya_model()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
    else:
        # Ollama serves /v1/systemone from its native root; the OpenAI-
        # compatible OLLAMA_BASE_URL carries a /v1 suffix that must be stripped.
        base = ollama_setup.native_base_url()
        model = laya_model()

    return {"provider": provider, "url": _systemone_url(base), "headers": headers, "model": model}


# ---------------------------------------------------------------------------
# Local backend: the open-source `laya` package runs in this process, no
# Ollama or network API needed. Used automatically when Ollama is too old
# for /v1/systemone, which is the "works by itself" path.
# ---------------------------------------------------------------------------

_local_router = None
_local_router_lock: asyncio.Lock | None = None
_local_install_task: asyncio.Task | None = None
_local_install_started = False
_ollama_upgrade_task: asyncio.Task | None = None
_ollama_upgrade_started = False

# Model weights are ~850 MB; require comfortable headroom for the download.
LOCAL_MIN_FREE_DISK_BYTES = 1_500_000_000


def local_model_name() -> str:
    """Checkpoint the local backend uses: english (421M), multilingual, or typed-decisions."""
    return _env("LAYA_LOCAL_MODEL", "english") or "english"


def local_package_available() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("laya") is not None
    except Exception:
        return False


def _free_disk_bytes() -> int:
    try:
        import shutil

        return shutil.disk_usage(app_paths.data_root()).free
    except Exception:
        return 0


def local_weights_downloaded() -> bool:
    """True once the checkpoint is present in the local HF cache."""
    if not local_package_available():
        return False
    try:
        from laya import DEFAULT_MODELS

        repo, revision = DEFAULT_MODELS.get(local_model_name(), DEFAULT_MODELS["english"])
        from huggingface_hub import snapshot_download  # noqa: F401

        cache = snapshot_download(repo_id=repo, revision=revision, local_files_only=True)
        return bool(cache)
    except Exception:
        return False


def local_status() -> dict:
    """Health of the in-process backend, for availability and the Settings card."""
    if not local_package_available():
        return {"available": False, "reason": "package_missing", "detail": (
            f"The built-in model's `laya` Python package is not installed in this "
            f"TALOS venv ({_venv_path()})."
        )}
    free = _free_disk_bytes()
    if free and free < LOCAL_MIN_FREE_DISK_BYTES:
        return {"available": False, "reason": "no_disk_space", "detail": (
            f"The built-in model needs about 1.5 GB of free disk space for its weights "
            f"({free / 1_000_000_000:.1f} GB free now). Free up disk space and try again."
        )}
    if not local_weights_downloaded():
        return {"available": True, "reason": "weights_pending", "detail": (
            "Built-in model ready; its ~850 MB weights download automatically on first use."
        )}
    return {"available": True, "reason": "ok", "detail": "Built-in laya model ready (runs in this process)."}


def _venv_path() -> str:
    """Which Python environment the built-in model installs into."""
    try:
        import sys

        return os.path.dirname(os.path.dirname(sys.executable))
    except Exception:
        return "unknown"


def _maybe_start_local_install() -> None:
    """Self-healing: install the `laya` package into the venv once, in the background."""
    global _local_install_task, _local_install_started
    if _local_install_started or local_package_available():
        return
    if _env("LAYA_AUTO_INSTALL", "1") == "0":
        return
    _local_install_started = True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    # Record the operation synchronously so an immediate status check sees it.
    set_operation_progress(
        "install_local",
        message=f"Installing the built-in model into the TALOS venv ({_venv_path()})...",
    )
    _local_install_task = loop.create_task(_local_install_worker())
    logger.warning(
        "The `laya` package is missing — installing it into this venv in the background "
        "(self-healing; disable with LAYA_AUTO_INSTALL=0)."
    )


async def _local_install_worker() -> None:
    import subprocess
    import sys

    set_operation_progress(
        "install_local",
        message=f"Installing the built-in model into the TALOS venv ({_venv_path()})...",
    )
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "laya"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=1800)
        if proc.returncode == 0:
            set_operation_progress(
                "install_local",
                message="Built-in model package installed; its ~850 MB weights download on first use.",
                done=True,
            )
            logger.info("The `laya` package installed successfully; the built-in decision backend is ready.")
        else:
            tail = (out or b"").decode(errors="replace")[-500:]
            set_operation_progress("install_local", error=tail or "pip install failed", done=True)
            logger.error(
                "Auto-install of the `laya` package failed (exit %s): %s — install it manually with "
                "`%s -m pip install laya` or use the Ollama/Jev backend.",
                proc.returncode,
                tail,
                sys.executable,
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        set_operation_progress("install_local", error=str(exc)[:300], done=True)
        logger.exception("Unexpected error while installing the `laya` package")


async def _get_local_router():
    """Lazily build the Router (downloads weights on first use); safe under concurrency."""
    global _local_router, _local_router_lock
    if _local_router is not None:
        return _local_router
    if _local_router_lock is None:
        _local_router_lock = asyncio.Lock()
    async with _local_router_lock:
        if _local_router is not None:
            return _local_router
        from laya import Router  # noqa: WPS433 - intentionally lazy and heavy

        def _build():
            router = Router(default=local_model_name())
            router.load(local_model_name())  # force the weight download here, not mid-request
            return router

        _local_router = await asyncio.to_thread(_build)
        return _local_router


async def _local_decide(state: Any, questions: Any) -> dict:
    """Run one decision through the in-process laya package (same wire format)."""
    normalized = normalize_questions(questions)
    if isinstance(state, (dict, list)):
        serialized = json.dumps(state, ensure_ascii=False, default=str)
        if len(serialized) > MAX_STATE_CHARS:
            raise LayaError(
                f"State is {len(serialized)} chars serialized; the cap is {MAX_STATE_CHARS}. Trim it to the part the questions are about."
            )
        state_value: Any = state
    else:
        state_value = str(state or "")[:MAX_STATE_CHARS]

    status = local_status()
    if not status.get("available"):
        raise LayaUnavailableError(status.get("detail") or "Local decision backend unavailable.")

    started = time.monotonic()
    router = await _get_local_router()
    result = await asyncio.to_thread(router.predict, state_value, normalized, model=local_model_name())
    latency_ms = round((time.monotonic() - started) * 1000.0, 1)

    answers = _extract_answers(result)
    if answers is None:
        raise LayaError(f"Local decision model returned no answers: {json.dumps(result, default=str)[:300]}")
    return {"answers": answers, "provider": "local", "model": local_model_name(), "latency_ms": latency_ms, "raw": result}


# ---------------------------------------------------------------------------
# The decision call
# ---------------------------------------------------------------------------


def _build_payload(state: Any, questions: Any, model: str | None) -> dict:
    if isinstance(state, (dict, list)):
        serialized = json.dumps(state, ensure_ascii=False, default=str)
        if len(serialized) > MAX_STATE_CHARS:
            raise LayaError(
                f"State is {len(serialized)} chars serialized; the cap is {MAX_STATE_CHARS}. Trim it to the part the questions are about."
            )
        state_value: Any = state
    else:
        state_value = str(state or "")[:MAX_STATE_CHARS]

    payload = {"state": state_value, "questions": normalize_questions(questions)}
    if model:
        payload["model"] = model
    return payload


def _normalize_answer(answer: Any) -> dict:
    if not isinstance(answer, dict):
        return {"value": answer}
    out = dict(answer)
    for numeric in ("noul", "confidence", "score"):
        if numeric in out:
            try:
                out[numeric] = float(out[numeric])
            except (TypeError, ValueError):
                pass
    return out


def _extract_answers(data: Any) -> dict | None:
    if not isinstance(data, dict):
        return None
    answers = data.get("answers")
    if not isinstance(answers, dict):
        answers = data.get("results")
    if not isinstance(answers, dict):
        return None
    return {str(k): _normalize_answer(v) for k, v in answers.items()}


async def decide(state: Any, questions: Any) -> dict:
    """Run one typed-decision call. Raises LayaError on any failure."""
    if not enabled():
        raise LayaUnavailableError("Laya decision layer is disabled (LAYA_ENABLED=0).")

    provider = await effective_provider()
    if provider == "local":
        return await _local_decide(state, questions)

    endpoint = provider_endpoint(provider)
    if not endpoint["url"]:
        raise LayaUnavailableError("No decision-model endpoint configured.")

    payload = _build_payload(state, questions, endpoint["model"])

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s())) as client:
            resp = await client.post(endpoint["url"], json=payload, headers=endpoint["headers"])
    except httpx.HTTPError as exc:
        raise LayaError(f"Decision model request failed: {exc}") from exc

    if resp.status_code != 200:
        raise LayaError(f"Decision model returned HTTP {resp.status_code}: {resp.text[:300]}")

    try:
        data = resp.json()
    except ValueError as exc:
        raise LayaError("Decision model returned invalid JSON.") from exc

    answers = _extract_answers(data)
    if answers is None:
        raise LayaError(f"Decision model response missing answers: {json.dumps(data, default=str)[:300]}")

    return {
        "answers": answers,
        "provider": endpoint["provider"],
        "model": endpoint["model"],
        "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
        "raw": data,
    }


_warn_state: dict[str, float] = {}
_last_failure: dict = {"feature": "", "error": "", "at": ""}


def _warn_failure(feature: str | None, exc: Exception) -> None:
    key = feature or "general"
    now = time.monotonic()
    _last_failure.update(
        feature=key,
        error=str(exc)[:500],
        at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    if now - _warn_state.get(key, 0.0) >= WARN_INTERVAL_S:
        _warn_state[key] = now
        logger.warning(
            "Laya decision layer failed (%s): %s — falling back to default behavior.",
            key,
            exc,
        )


def _log_decision(feature: str | None, result: dict, questions: Any) -> None:
    try:
        question_types = {
            str(k): (v.get("type") if isinstance(v, dict) else "?")
            for k, v in (questions or {}).items()
        }
        answers = {
            k: {kk: vv for kk, vv in v.items() if kk in ("choice", "noul", "score", "confidence")}
            for k, v in (result.get("answers") or {}).items()
        }
        entry = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "feature": feature or "direct",
            "provider": result.get("provider"),
            "model": result.get("model"),
            "latency_ms": result.get("latency_ms"),
            "questions": question_types,
            "answers": answers,
        }
        decision_logger.info(json.dumps(entry, default=str))
    except Exception:
        logger.debug("Could not write laya decision log entry", exc_info=True)


async def try_decide(state: Any, questions: Any, feature: str | None = None) -> dict | None:
    """Fail-open decide(): returns None on any error so callers keep today's behavior."""
    try:
        result = await decide(state, questions)
        _log_decision(feature, result, questions)
        return result
    except LayaError as exc:
        _warn_failure(feature, exc)
        return None
    except Exception as exc:
        _warn_failure(feature, exc)
        logger.exception("Unexpected error in laya decision layer")
        return None


# ---------------------------------------------------------------------------
# Availability (cached) and Ollama self-healing
# ---------------------------------------------------------------------------


def _parse_version(version: str) -> tuple[int, int, int] | None:
    parts = version.strip().split(".")
    if len(parts) < 2:
        return None
    try:
        nums = [int(p.split("-")[0]) for p in parts[:3]]
    except (ValueError, IndexError):
        return None
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


_availability_cache: dict = {"checked_at": 0.0, "result": None}
_pull_started_for: set[str] = set()
_pull_task: asyncio.Task | None = None

# ---------------------------------------------------------------------------
# Live operation state — one place the Settings UI polls so every download,
# upgrade, and install (background or button-triggered) shows clean progress.
# ---------------------------------------------------------------------------

_op_state: dict = {
    "operation": None,      # "pull" | "upgrade_ollama" | "install_local" | "test" | None
    "percent": None,        # 0-100 float, or None for indeterminate work
    "message": "",
    "error": "",
    "done": False,
    "updated_at": 0.0,
    "started_at": 0.0,
}


def set_operation_progress(
    operation: str | None,
    percent: float | None = None,
    message: str = "",
    error: str = "",
    done: bool = False,
) -> None:
    """Record the current install/upgrade/download progress for the status API."""
    now = time.time()
    previous = _op_state.get("operation")
    # A genuinely new operation (different id, or restarting after done) gets a
    # fresh start time so the UI can show how long it has been running.
    new_start = (
        operation
        and operation != previous
        or (operation and _op_state.get("done"))
    )
    _op_state.update(
        operation=operation,
        percent=(round(float(percent), 1) if isinstance(percent, (int, float)) else None),
        message=str(message or "")[:300],
        error=str(error or "")[:500],
        done=bool(done),
        updated_at=now,
        started_at=now if new_start else _op_state.get("started_at") or now,
    )


def operation_state() -> dict:
    """Current operation progress; active is False when nothing is running."""
    state = dict(_op_state)
    active = bool(state.get("operation")) and not state.get("done") and (
        time.time() - float(state.get("updated_at") or 0.0)
    ) < 1800.0  # stale after 30 minutes without an update
    state["active"] = active
    started_at = float(state.get("started_at") or 0.0)
    state["elapsed_s"] = max(0, int(time.time() - started_at)) if active else 0
    return state


def clear_operation(operation: str) -> None:
    """Mark an operation finished so the UI stops showing progress for it."""
    if _op_state.get("operation") == operation:
        _op_state["done"] = True
        _op_state["updated_at"] = time.time()


def invalidate_availability() -> None:
    """Drop the cached provider health so the next call re-probes."""
    _availability_cache["checked_at"] = 0.0
    _availability_cache["result"] = None


def _maybe_start_pull(model: str) -> None:
    """Kick off a one-shot background `ollama pull` for the laya model."""
    global _pull_task
    if model in _pull_started_for:
        return
    _pull_started_for.add(model)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    # Record the operation synchronously so an immediate status check sees it.
    set_operation_progress("pull", percent=0, message=f"Downloading the {model} decision model (~850 MB)...")
    _pull_task = loop.create_task(_auto_pull_worker(model))
    logger.warning(
        "Laya model %r is missing from Ollama — pulling it in the background. "
        "Disable this self-healing with LAYA_AUTO_PULL=0.",
        model,
    )


async def _auto_pull_worker(model: str) -> None:
    last_pct = -1.0
    set_operation_progress("pull", percent=0, message=f"Downloading the {model} decision model (~850 MB)...")
    try:
        async for event in ollama_setup.pull_model(model):
            stage = event.get("stage")
            if stage == "error":
                set_operation_progress("pull", error=str(event.get("error"))[:300], done=True)
                logger.error(
                    "Auto-pull of laya model %r failed: %s — install it manually with `ollama pull %s` or use a JEV_API_KEY backend.",
                    model,
                    str(event.get("error"))[:300],
                    model,
                )
                return
            pct = event.get("percent")
            if isinstance(pct, (int, float)) and float(pct) - last_pct >= 2.0:
                last_pct = float(pct)
                set_operation_progress("pull", percent=last_pct, message=f"Downloading {model}: {int(last_pct)}%")
                if float(pct) - last_pct >= 25.0:
                    logger.info("Pulling laya model %s: %d%%", model, int(pct))
        set_operation_progress("pull", percent=100, message=f"{model} installed.", done=True)
        logger.info("Laya model %s installed successfully.", model)
        ollama_setup.forget_model_info()
        invalidate_availability()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        set_operation_progress("pull", error=str(exc)[:300], done=True)
        logger.exception("Unexpected error while pulling laya model %r", model)


async def _probe_ollama() -> dict:
    """Health of the Ollama backend: reachable, 0.40+, model installed."""
    status = await ollama_setup.get_status()
    if not status.get("reachable"):
        return {"available": False, "provider": "ollama", "reason": "ollama_unreachable",
                "detail": status.get("error", "") or "Ollama is not reachable."}

    version = str(status.get("version", ""))
    parsed = _parse_version(version)
    if parsed is not None and parsed < MIN_OLLAMA_VERSION:
        return {"available": False, "provider": "ollama", "reason": "ollama_too_old", "detail": (
            f"Ollama {version} is installed, but the laya decision model needs Ollama 0.40.0+ "
            "(the /v1/systemone endpoint)."
        )}

    model = laya_model()
    installed = {str(m.get("name", "")) for m in status.get("models", [])}
    model_present = any(
        name == model or name.split(":")[0] == model for name in installed
    )
    if not model_present:
        if auto_pull_enabled():
            _maybe_start_pull(model)
            return {"available": False, "provider": "ollama", "reason": "pulling",
                    "detail": f"Pulling {model} via Ollama in the background (one-time, ~850 MB)."}
        return {"available": False, "provider": "ollama", "reason": "model_missing", "detail": (
            f"Run `ollama pull {model}` (Settings -> Decision Model can do it for you)."
        )}

    return {"available": True, "provider": "ollama", "reason": "ok",
            "detail": f"Ollama {version} with {model}."}


def ollama_auto_update_enabled() -> bool:
    return _env("LAYA_AUTO_UPDATE_OLLAMA", "1") != "0"


def claim_ollama_upgrade() -> bool:
    """Reserve the Ollama upgrade so the button and the self-healer never race.

    Returns True when the caller owns the upgrade; False when one is already
    active (started by the other path).
    """
    global _ollama_upgrade_started
    op = operation_state()
    if op.get("active") and op.get("operation") == "upgrade_ollama":
        return False
    _ollama_upgrade_started = True  # the background self-healer defers to this run
    return True


def claim_laya_pull() -> bool:
    """Reserve the model pull; False when a pull is already running."""
    op = operation_state()
    return not (op.get("active") and op.get("operation") == "pull")


def _maybe_start_ollama_upgrade() -> None:
    """Self-healing: upgrade Ollama once in the background when it is too old.

    This is what makes the layer "work by itself": old Ollama triggers the
    upgrade, the upgrade unblocks /v1/systemone, and the laya model then
    auto-pulls. Disabled with LAYA_AUTO_UPDATE_OLLAMA=0.
    """
    global _ollama_upgrade_task, _ollama_upgrade_started
    if _ollama_upgrade_started or not ollama_auto_update_enabled():
        return
    _ollama_upgrade_started = True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    # Record the operation synchronously so an immediate status check sees it.
    set_operation_progress("upgrade_ollama", message="Upgrading Ollama...")
    _ollama_upgrade_task = loop.create_task(_ollama_upgrade_worker())
    logger.warning(
        "Ollama is too old for the laya decision model — upgrading Ollama "
        "automatically in the background (self-healing; disable with "
        "LAYA_AUTO_UPDATE_OLLAMA=0)."
    )


async def _ollama_upgrade_worker() -> None:
    set_operation_progress("upgrade_ollama", message="Upgrading Ollama...")
    try:
        async for event in ollama_setup.upgrade_ollama():
            stage = event.get("stage")
            if stage == "error":
                set_operation_progress("upgrade_ollama", error=str(event.get("error"))[:400], done=True)
                logger.error(
                    "Automatic Ollama upgrade failed: %s — use Settings -> Decision Model "
                    "or https://ollama.com to upgrade manually.",
                    str(event.get("error"))[:400],
                )
                return
            if stage in {"progress", "start"}:
                set_operation_progress(
                    "upgrade_ollama",
                    percent=event.get("percent"),
                    message=str(event.get("message") or "Upgrading Ollama..."),
                )
            if stage == "done":
                set_operation_progress(
                    "upgrade_ollama",
                    percent=100,
                    message=str(event.get("message") or "Ollama upgraded."),
                    done=True,
                )
                logger.info("Ollama upgraded automatically: %s", event.get("message", ""))
                ollama_setup.forget_model_info()
                invalidate_availability()
                return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        set_operation_progress("upgrade_ollama", error=str(exc)[:300], done=True)
        logger.exception("Unexpected error while upgrading Ollama")


def _local_probe_for_missing_package() -> dict | None:
    """Kick off the self-healing package install and describe it, if allowed."""
    if _env("LAYA_AUTO_INSTALL", "1") == "0":
        return None
    _maybe_start_local_install()
    return {"available": False, "provider": "local", "reason": "installing", "detail": (
        f"Installing the built-in model into the TALOS venv ({_venv_path()}) in the "
        "background — one-time; its ~850 MB weights download on first use."
    )}


async def effective_provider() -> str:
    """The backend that actually answers decisions right now.

    Explicit choices (jev, custom, local) are honored as-is. Auto and ollama
    use Ollama when it is new enough and has the model; otherwise auto falls
    back to the built-in package so the decision layer keeps working on
    installs with an older Ollama.
    """
    pref = resolve_provider()
    if pref in {"jev", "custom", "local"}:
        return pref
    if pref == "auto":
        if _env("LAYA_BASE_URL"):
            return "custom"
        if _env("JEV_API_KEY"):
            return "jev"

    status = await availability()
    if status.get("available"):
        provider = str(status.get("provider") or "ollama")
        return provider if provider in {"ollama", "local", "jev", "custom"} else "ollama"
    return "local" if pref == "auto" else pref


async def _probe_availability() -> dict:
    if not enabled():
        return {"available": False, "provider": None, "reason": "disabled", "detail": "LAYA_ENABLED=0"}

    provider = resolve_provider()
    try:
        if provider == "jev":
            if not _env("JEV_API_KEY"):
                return {"available": False, "provider": "jev", "reason": "missing_key",
                        "detail": "Set JEV_API_KEY (Settings -> Decision Model) to use the hosted Jev backend."}
            return {"available": True, "provider": "jev", "reason": "ok", "detail": "Hosted Jev API key configured."}

        if provider == "custom":
            url = _systemone_url(_env("LAYA_BASE_URL") or _env("JEV_BASE_URL", DEFAULT_JEV_BASE_URL))
            if not url:
                return {"available": False, "provider": "custom", "reason": "missing_url",
                        "detail": "Set LAYA_BASE_URL to a Jev-compatible /v1/systemone endpoint."}
            return {"available": True, "provider": "custom", "reason": "ok", "detail": f"Custom endpoint {url}."}

        if provider == "local":
            local = local_status()
            if not local.get("available") and local.get("reason") == "package_missing":
                installing = _local_probe_for_missing_package()
                if installing:
                    return installing
            return {"available": bool(local.get("available")), "provider": "local",
                    "reason": local.get("reason"), "detail": local.get("detail")}

        # ollama (explicit) or auto
        ollama_result = await _probe_ollama()
        if ollama_result.get("available"):
            return ollama_result

        # An Ollama that is too old is fixed by upgrading it — automatically,
        # unless LAYA_AUTO_UPDATE_OLLAMA=0. The laya model is pulled right
        # after the upgrade, so the whole chain self-heals.
        upgrading_ollama = (
            ollama_result.get("reason") == "ollama_too_old" and ollama_auto_update_enabled()
        )
        if upgrading_ollama:
            _maybe_start_ollama_upgrade()

        if provider == "ollama":
            if upgrading_ollama:
                return {"available": False, "provider": "ollama", "reason": "upgrading_ollama", "detail": (
                    f"{ollama_result.get('detail')} Upgrading Ollama automatically in the "
                    "background; the laya model is pulled right after."
                )}
            return ollama_result

        # Auto: Ollama cannot serve the decision model (old version, missing
        # model, or not running) — fall back to the built-in package so the
        # decision layer works without any manual setup.
        local = local_status()
        if local.get("available"):
            detail = f"{local.get('detail')} (used because: {ollama_result.get('detail')})"
            if upgrading_ollama:
                detail += " Ollama is being upgraded in the background and will take over once ready."
            return {"available": True, "provider": "local", "reason": "local_fallback", "detail": detail}
        if local.get("reason") == "package_missing":
            installing = _local_probe_for_missing_package()
            if installing:
                return installing
        if upgrading_ollama:
            return {"available": False, "provider": "local", "reason": "upgrading_ollama", "detail": (
                f"{local.get('detail')} Meanwhile, Ollama is being upgraded automatically in the "
                "background and the laya model will be pulled once it is ready."
            )}
        return {"available": False, "provider": "local", "reason": local.get("reason"),
                "detail": f"{local.get('detail')} (Ollama: {ollama_result.get('detail')})"}
    except Exception as exc:
        return {"available": False, "provider": provider, "reason": "error", "detail": str(exc)}


async def availability(force: bool = False) -> dict:
    """Provider health, cached for AVAILABILITY_TTL_S. Never raises."""
    now = time.monotonic()
    cached = _availability_cache["result"]
    if not force and cached and now - _availability_cache["checked_at"] < AVAILABILITY_TTL_S:
        return cached
    result = await _probe_availability()
    _availability_cache["checked_at"] = now
    _availability_cache["result"] = result
    return result


async def available() -> bool:
    return bool((await availability()).get("available"))


# ---------------------------------------------------------------------------
# Settings support
# ---------------------------------------------------------------------------


async def test_decision() -> dict:
    """One real decision call with known input, for the Settings Test button."""
    state = (
        "Our API integration started returning 500 errors on every request "
        "about 20 minutes ago, and we cannot process any customer orders "
        "until this is fixed."
    )
    questions = {
        "department": choice(
            "Which team should handle this?",
            {
                "technical": "bugs, outages, system errors",
                "billing": "invoices, payments, refunds",
                "other": "everything else",
            },
        ),
        "urgent": noul("The message conveys urgency or time-sensitivity."),
        "frustration": score(
            "How frustrated the sender appears",
            ["calm, stating facts", "frustrated but civil", "very angry, strong language"],
        ),
    }
    # A backend that is still being set up should say so, not "test failed".
    provider = await effective_provider()
    if provider == "local" and not local_package_available():
        installing = _local_probe_for_missing_package()
        if installing:
            return {
                "ok": False,
                "error": (
                    "The built-in model is being installed into the TALOS venv right now "
                    "(one-time; a few minutes). Try the test again shortly."
                ),
                "operation": operation_state(),
            }
        return {
            "ok": False,
            "error": (
                f"The built-in model's `laya` package is not installed in this venv "
                f"({_venv_path()}). Enable 'Install the built-in model automatically' "
                "in Settings, or switch provider to Ollama or Jev."
            ),
            "availability": await availability(force=True),
        }
    operation = operation_state()
    if operation.get("active") and operation.get("operation") == "pull":
        return {
            "ok": False,
            "error": "The decision model is still downloading. Try the test again once it finishes.",
            "operation": operation,
        }

    try:
        result = await decide(state, questions)
        _log_decision("settings_test", result, questions)
        return {
            "ok": True,
            "provider": result["provider"],
            "model": result["model"],
            "latency_ms": result["latency_ms"],
            "answers": {
                k: {kk: vv for kk, vv in v.items() if kk in ("choice", "noul", "score", "confidence")}
                for k, v in result["answers"].items()
            },
        }
    except LayaError as exc:
        return {"ok": False, "error": str(exc), "availability": await availability(force=True)}


def last_failure() -> dict:
    """The most recent fail-open error, for the Settings status card."""
    return dict(_last_failure)


def mark_pull_requested(model: str) -> None:
    """Register a manual pull so the background self-healer does not double-pull."""
    _pull_started_for.add(model)


def pull_progress() -> dict:
    """Live operation progress for the Settings status card."""
    task = _pull_task
    local_task = _local_install_task
    return {
        "pulling": bool(task and not task.done()),
        "pull_requested": bool(_pull_started_for),
        "installing_local": bool(local_task and not local_task.done()),
        "local_install_requested": bool(_local_install_started),
        "upgrading_ollama": bool(_ollama_upgrade_task and not _ollama_upgrade_task.done()),
        "ollama_upgrade_requested": bool(_ollama_upgrade_started),
        "operation": operation_state(),
    }


# ---------------------------------------------------------------------------
# Self-test — the offline contract of the decision layer, run by `clai check`
# and the dashboard System Check. Fast, deterministic, no model calls.
# ---------------------------------------------------------------------------


def self_test() -> dict:
    """Run the layer's offline self-tests. Returns {"ok", "passed", "failed", "failures"}."""
    import importlib.util

    checks: list[tuple[str, callable]] = [
        ("question_builders", lambda: (
            choice("Which?", {"a": "A", "b": "B"}) == {"type": "choice", "instructions": "Which?", "criteria": {"a": "A", "b": "B"}}
            and noul("Is it?") == {"type": "noul", "instructions": "Is it?"}
            and score("How?", ["lo", "hi"])["criteria"] == ["lo", "hi"]
        )),
        ("question_normalization", lambda: (
            normalize_questions({
                "dept": choice("Which?", {"tech": "bugs"}),
                "urgent": noul("Urgent?"),
            })["dept"]["criteria"] == {"tech": "bugs"}
        )),
        ("question_rejection", lambda: (
            _raises(lambda: normalize_questions({"q": {"type": "choice", "instructions": "?"}}))
            and _raises(lambda: normalize_questions({"q": {"type": "noul"}}))
            and _raises(lambda: normalize_questions("nope"))
        )),
        ("payload_state_passthrough", lambda: (
            _build_payload({"a": 1}, {"q": noul("?")}, "laya")["state"] == {"a": 1}
        )),
        ("payload_state_truncation", lambda: (
            len(_build_payload("x" * 60000, {"q": noul("?")}, None)["state"]) == MAX_STATE_CHARS
        )),
        ("payload_rejects_huge_structured_state", lambda: (
            _raises(lambda: _build_payload({"blob": "x" * 60000}, {"q": noul("?")}, None))
        )),
        ("response_extraction", lambda: (
            (_extract_answers({"answers": {"q": {"noul": "0.9"}}}) or {}).get("q", {}).get("noul") == 0.9
            and _extract_answers({"nope": 1}) is None
        )),
        ("version_parsing", lambda: (
            _parse_version("0.32.14") == (0, 32, 14)
            and _parse_version("0.40.0") == (0, 40, 0)
            and _parse_version("garbage") is None
        )),
        ("systemone_url_forms", lambda: (
            _systemone_url("http://h:1") == "http://h:1/v1/systemone"
            and _systemone_url("https://x/v1/") == "https://x/v1/systemone"
            and _systemone_url("") == ""
        )),
        ("feature_gating", lambda: (
            feature_enabled("laya_self_test_nonexistent") is False
        )),
        ("confidence_bounds", lambda: (
            abs(confidence_min() - 0.75) < 1e-9 or 0.0 <= confidence_min() <= 1.0
        )),
        ("package_present", lambda: (
            importlib.util.find_spec("laya") is not None
        )),
    ]

    passed = 0
    failures: list[str] = []
    for name, fn in checks:
        try:
            if fn():
                passed += 1
            else:
                failures.append(f"{name}: returned an unexpected result")
        except Exception as exc:
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    # Fail-open contract: a disabled or broken backend must yield None, never raise.
    # Runs in its own thread with its own event loop so it works even while
    # the app's loop is running (diagnostics run inside the live process).
    def _fail_open_check() -> bool:
        original = os.environ.get("LAYA_ENABLED")
        os.environ["LAYA_ENABLED"] = "0"
        try:
            return asyncio.run(try_decide("x", {"q": noul("?")}, feature="self_test")) is None
        except Exception:
            return False
        finally:
            if original is None:
                os.environ.pop("LAYA_ENABLED", None)
            else:
                os.environ["LAYA_ENABLED"] = original

    import threading

    outcome: dict[str, bool] = {"ok": False}

    def _run_fail_open() -> None:
        outcome["ok"] = _fail_open_check()

    try:
        t = threading.Thread(target=_run_fail_open, daemon=True)
        t.start()
        t.join(timeout=10.0)
        if outcome["ok"]:
            passed += 1
        else:
            failures.append("fail_open: try_decide did not fail open while disabled")
    except Exception as exc:
        failures.append(f"fail_open: {type(exc).__name__}: {exc}")

    return {
        "ok": not failures,
        "passed": passed,
        "total": len(checks) + 1,
        "failures": failures,
    }


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except LayaError:
        return True
