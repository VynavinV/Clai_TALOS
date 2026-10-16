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
    """Which backend answers decisions: ollama, jev, or a custom endpoint."""
    pref = _env("LAYA_PROVIDER", "auto").lower()
    if pref in {"ollama", "jev", "custom"}:
        return pref
    # Auto: an explicit endpoint is the most specific, then a hosted key,
    # then the local Ollama every TALOS install can fall back to.
    if _env("LAYA_BASE_URL"):
        return "custom"
    if _env("JEV_API_KEY"):
        return "jev"
    return "ollama"


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

    endpoint = provider_endpoint()
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
    _pull_task = loop.create_task(_auto_pull_worker(model))
    logger.warning(
        "Laya model %r is missing from Ollama — pulling it in the background. "
        "Disable this self-healing with LAYA_AUTO_PULL=0.",
        model,
    )


async def _auto_pull_worker(model: str) -> None:
    last_pct = -1.0
    try:
        async for event in ollama_setup.pull_model(model):
            stage = event.get("stage")
            if stage == "error":
                logger.error(
                    "Auto-pull of laya model %r failed: %s — install it manually with `ollama pull %s` or use a JEV_API_KEY backend.",
                    model,
                    str(event.get("error"))[:300],
                    model,
                )
                return
            pct = event.get("percent")
            if isinstance(pct, (int, float)) and float(pct) - last_pct >= 25.0:
                last_pct = float(pct)
                logger.info("Pulling laya model %s: %d%%", model, int(pct))
        logger.info("Laya model %s installed successfully.", model)
        ollama_setup.forget_model_info()
        invalidate_availability()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Unexpected error while pulling laya model %r", model)


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

        # ollama
        status = await ollama_setup.get_status()
        if not status.get("reachable"):
            return {"available": False, "provider": "ollama", "reason": "ollama_unreachable",
                    "detail": status.get("error", "") or "Ollama is not reachable."}

        version = str(status.get("version", ""))
        parsed = _parse_version(version)
        if parsed is not None and parsed < MIN_OLLAMA_VERSION:
            return {"available": False, "provider": "ollama", "reason": "ollama_too_old", "detail": (
                f"Ollama {version} is installed, but the laya decision model needs Ollama 0.40.0+ "
                "(the /v1/systemone endpoint). Upgrade from https://ollama.com, or set JEV_API_KEY "
                "to use the hosted Jev model instead."
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
                f"Run `ollama pull {model}` (Settings -> Decision Model can do it for you), "
                "or set JEV_API_KEY for the hosted backend."
            )}

        return {"available": True, "provider": "ollama", "reason": "ok",
                "detail": f"Ollama {version} with {model}."}
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
    """Whether a background auto-pull is running, for the Settings status card."""
    task = _pull_task
    return {
        "pulling": bool(task and not task.done()),
        "pull_requested": bool(_pull_started_for),
    }
