"""Mobile app API — glasses pairing, message ingestion, and the long-run
event channel that "pings the glasses" when a run finishes or follows up.

Endpoints (bearer-token auth, for the Android app):
    GET  /api/mobile/status        — lightweight capability/config probe
    POST /api/mobile/message       — submit a run, returns 202 + run_id immediately
    GET  /api/mobile/events        — long-poll the run's event queue (cursor resume)
    POST /api/mobile/events/ack    — mark events delivered

Endpoints (dashboard session+CSRF auth, for the settings card):
    GET  /api/mobile/tokens        — list tokens + glasses model + config
    POST /api/mobile/tokens        — create token (shown once) + pairing QR payload
    POST /api/mobile/tokens/revoke — revoke a token
    POST /api/mobile/settings      — glasses model + speak/mirror toggles

Why submit-then-stream: the web chat awaits the whole run inside one HTTP
request, which is fine for a browser but unusable for a 10–20 minute mobile
run. Mobile messages return 202 immediately; every send_func output is
persisted to mobile_events and the app long-polls for them. Meta exposes no
server→glasses push, so the phone app is the bridge and the queue must
survive drops, phone locks, and app restarts — hence monotonic ids + ack.

Speech through the glasses is additive: Telegram mirroring (and every other
channel) behaves exactly as it does for cron-originated runs. Only runs
submitted through this API tag their events speak=true.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import time
import weakref
from urllib.parse import quote

from aiohttp import web

import db

logger = logging.getLogger("talos.mobile")
security_logger = logging.getLogger("talos.security")

GLASSES_MODELS = [
    "Ray-Ban Meta Gen 1",
    "Ray-Ban Meta Gen 2",
    "Ray-Ban Meta Gen 3",
    "Oakley Meta",
    "Other",
]
DEFAULT_GLASSES_MODEL = "Ray-Ban Meta Gen 3"
_GLASSES_MODEL_STATE_KEY = "mobile_glasses_model"

# Bearer auth failure policy, mirroring the dashboard login limits.
MAX_AUTH_FAILURES = 5
AUTH_LOCKOUT_SECONDS = 300

# Sliding-window rate limit per token (requests per minute).
DEFAULT_RATE_LIMIT_PER_MIN = 120

_PROVIDER_ENV_KEYS = (
    "ZHIPUAI_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "NVIDIA_API_KEY", "CEREBRAS_API_KEY", "GROQ_API_KEY", "QWEN_API_KEY",
    "OPENROUTER_API_KEY", "MISTRAL_API_KEY", "OTHER_API_KEY",
)

# Wiring filled in by setup_routes(); kept module-level so handlers and tests
# can reach the decorators/env helpers without importing telegram_bot.
_deps: dict = {}


def _read_env() -> dict:
    reader = _deps.get("read_env")
    if callable(reader):
        return reader()
    return {}


def _write_env(env_vars: dict) -> None:
    writer = _deps.get("write_env")
    if callable(writer):
        writer(env_vars)


def _request_origin(request) -> str:
    getter = _deps.get("request_origin")
    if callable(getter):
        return getter(request)
    proto = request.headers.get("X-Forwarded-Proto", request.scheme).strip() or request.scheme
    host = request.headers.get("X-Forwarded-Host", request.host).strip() or request.host
    return f"{proto}://{host}"


def _env_flag(key: str, default: str = "1") -> bool:
    env = _read_env()
    raw = str(env.get(key, default)).strip().lower()
    return raw in ("1", "true", "on", "yes")


def _env_int(key: str, default: int) -> int:
    raw = str(_read_env().get(key, "")).strip()
    if not raw:
        return int(default)
    try:
        return int(raw)
    except ValueError:
        return int(default)


# ---------------------------------------------------------------------------
# Auth: bearer tokens, hashed at rest, rate-limited, lockout on failures.
# ---------------------------------------------------------------------------

_auth_failures: dict[str, list[float]] = {}
_auth_lockouts: dict[str, float] = {}
_rate_windows: dict[int, list[float]] = {}


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _prune_stamps(stamps: list[float], window_s: float) -> list[float]:
    now = time.monotonic()
    return [t for t in stamps if now - t <= window_s]


def _record_failure(key: str) -> None:
    stamps = _prune_stamps(_auth_failures.get(key, []), AUTH_LOCKOUT_SECONDS)
    stamps.append(time.monotonic())
    _auth_failures[key] = stamps
    if len(stamps) >= MAX_AUTH_FAILURES:
        _auth_lockouts[key] = time.monotonic() + AUTH_LOCKOUT_SECONDS
        security_logger.warning("mobile API lockout for %s after %d failed auths", key, len(stamps))


def _is_locked_out(key: str) -> bool:
    until = _auth_lockouts.get(key)
    if until is None:
        return False
    if time.monotonic() >= until:
        _auth_lockouts.pop(key, None)
        _auth_failures.pop(key, None)
        return False
    return True


def _clear_failures(key: str) -> None:
    _auth_failures.pop(key, None)
    _auth_lockouts.pop(key, None)


def _is_private_remote(request) -> bool:
    host = request.remote or ""
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback
    except ValueError:
        return False


def _enforce_https_guard(request):
    """Plain HTTP is only allowed from localhost/LAN unless opted out.

    Tailscale serve/funnel terminates TLS in front of the app, so the
    X-Forwarded-Proto header is trusted when present.
    """
    if _env_flag("MOBILE_ALLOW_HTTP", "0"):
        return None
    proto = request.headers.get("X-Forwarded-Proto", request.scheme).strip().lower() or request.scheme
    if proto == "https":
        return None
    if _is_private_remote(request):
        return None
    return web.json_response(
        {"error": "HTTPS required for mobile API access off-LAN. Use your Tailscale URL."},
        status=426,
    )


def require_app_token(handler):
    async def middleware(request):
        if not _env_flag("MOBILE_API_ENABLED", "1"):
            return web.json_response({"error": "mobile API disabled"}, status=503)

        https_block = _enforce_https_guard(request)
        if https_block is not None:
            return https_block

        remote = request.remote or "unknown"
        if _is_locked_out(f"ip:{remote}"):
            return web.json_response({"error": "locked out, try later"}, status=429)

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            _record_failure(f"ip:{remote}")
            return web.json_response({"error": "unauthorized"}, status=401)
        token = auth[len("Bearer "):].strip()
        if not token:
            _record_failure(f"ip:{remote}")
            return web.json_response({"error": "unauthorized"}, status=401)

        token_hash = _hash_token(token)
        row = db.find_app_token_by_hash(token_hash)
        if row is None or row.get("revoked"):
            _record_failure(f"ip:{remote}")
            security_logger.warning(
                "mobile API auth failed from %s (unknown or revoked token)", remote
            )
            return web.json_response({"error": "unauthorized"}, status=401)

        # Constant-time compare of the stored hash (the DB lookup above is by
        # hash; this keeps the comparison itself early-exit-free).
        if not hmac.compare_digest(str(row["token_hash"]), token_hash):
            _record_failure(f"ip:{remote}")
            return web.json_response({"error": "unauthorized"}, status=401)

        if _is_locked_out(f"token:{row['id']}"):
            return web.json_response({"error": "locked out, try later"}, status=429)

        # Per-token sliding-window rate limit.
        limit = _env_int("MOBILE_RATE_LIMIT_PER_MIN", DEFAULT_RATE_LIMIT_PER_MIN)
        now = time.monotonic()
        window = _prune_stamps(_rate_windows.get(row["id"], []), 60.0)
        if len(window) >= max(1, limit):
            _rate_windows[row["id"]] = window
            return web.json_response(
                {"error": "rate limited"},
                status=429,
                headers={"Retry-After": "10"},
            )
        window.append(now)
        _rate_windows[row["id"]] = window

        _clear_failures(f"ip:{remote}")
        request._mobile_token = row
        try:
            db.touch_app_token(int(row["id"]))
        except Exception:
            logger.debug("could not touch app token usage", exc_info=True)
        return await handler(request)

    return middleware


# ---------------------------------------------------------------------------
# Plain-English speak_text derivation (what the glasses say out loud).
# ---------------------------------------------------------------------------

_MD_CODE_FENCE = re.compile(r"```[\s\S]*?```")
_MD_INLINE_CODE = re.compile(r"`[^`\n]*`")
_URL_RE = re.compile(r"https?://\S+")
_MD_MARKUP = re.compile(r"[*_#>|]{1,3}")


def derive_speak_text(event_type: str, text: str, max_chars: int = 0) -> str:
    """Render an event as a short spoken sentence, not a screen dump.

    URLs become a note that the link is on Telegram, code and markup are
    stripped, and the result is capped so the glasses never read an essay.
    """
    if max_chars <= 0:
        max_chars = _env_int("MOBILE_SPEAK_MAX_CHARS", 300)

    raw = str(text or "").strip()
    if event_type in ("image", "document"):
        spoken = raw or "I've sent you a file."
        return spoken[:max_chars]
    if event_type == "voice":
        # voice events carry the script to speak; keep as-is after cleanup
        return raw[:max_chars]
    if event_type in ("status", "run_done", "error"):
        return raw[:max_chars]

    cleaned = _MD_CODE_FENCE.sub(" ", raw)
    cleaned = _MD_INLINE_CODE.sub(" ", cleaned)
    had_urls = bool(_URL_RE.search(cleaned))
    cleaned = _URL_RE.sub(" ", cleaned)
    cleaned = _MD_MARKUP.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if had_urls:
        cleaned = (cleaned + " The link is on Telegram.").strip()
    if not cleaned:
        return ""
    if len(cleaned) > max_chars:
        cleaned = cleaned[: max(1, max_chars - 1)].rstrip() + "…"
    return cleaned


# ---------------------------------------------------------------------------
# User resolution: mobile runs live under the primary Telegram user id so
# context, memories, and Telegram mirroring stay unified.
# ---------------------------------------------------------------------------

def resolve_primary_user_id() -> int:
    with db._conn() as conn:
        row = conn.execute(
            "SELECT user_id FROM user_profiles WHERE user_id > 0 ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        if row:
            return int(row["user_id"])
        row = conn.execute(
            "SELECT user_id FROM chat_history WHERE user_id > 0 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            return int(row["user_id"])
    return -1


# ---------------------------------------------------------------------------
# Event queue helpers + long-poll wakeup.
#
# asyncio primitives bind to the loop that first uses them, so loop-scoped
# state (condition + run locks) is keyed by the running loop. Production has
# one loop forever; tests get a fresh one per test.
# ---------------------------------------------------------------------------

_loop_state: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _state() -> dict:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        # Outside a loop (e.g. direct sync inspection): throwaway state.
        return {"cond": None, "locks": {}}
    st = _loop_state.get(loop)
    if st is None:
        st = {"cond": asyncio.Condition(), "locks": {}}
        _loop_state[loop] = st
    return st


def _events_cond() -> asyncio.Condition:
    return _state()["cond"]


def _enqueue_event(token_id: int, run_id: str, event_type: str, text: str, speak: bool) -> None:
    speak_text = derive_speak_text(event_type, text)
    db.add_mobile_event(
        token_id,
        run_id,
        event_type,
        text=str(text or "")[:20000],
        speak_text=speak_text,
        speak=1 if speak else 0,
    )
    try:
        db.prune_mobile_events(_env_int("MOBILE_EVENT_RETENTION_DAYS", 7))
    except Exception:
        logger.debug("mobile event prune failed", exc_info=True)


async def _notify_event_waiters() -> None:
    cond = _events_cond()
    if cond is None:
        return
    try:
        async with cond:
            cond.notify_all()
    except Exception:
        logger.debug("could not notify mobile event waiters", exc_info=True)


# ---------------------------------------------------------------------------
# Run execution: 202 immediately, work in the background under the same
# per-user lock semantics as the web chat (new message outranks old run).
# ---------------------------------------------------------------------------

def _get_run_lock(user_id: int) -> asyncio.Lock:
    locks = _state()["locks"]
    lock = locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        locks[user_id] = lock
    return lock


def _make_telegram_mirror(user_id: int):
    """Copy of the cron_jobs._make_send_func pattern: send text replies to the
    user's Telegram chat using the server-side bot token."""
    import os

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return None

    from telegram import Bot

    bot = Bot(token=token)

    async def mirror(text: str) -> None:
        stripped = (text or "").strip()
        if not stripped:
            return
        for i in range(0, len(stripped), 4096):
            chunk = stripped[i:i + 4096]
            try:
                await bot.send_message(chat_id=user_id, text=chunk)
            except Exception as exc:
                logger.warning("Mobile Telegram mirror failed for %d: %s", user_id, exc)
                break

    return mirror


def build_mobile_send_func(token_id: int, run_id: str, user_id: int, speak: bool):
    """send_func that feeds the app event queue and mirrors to Telegram.

    Mirroring is additive: with MOBILE_REPLY_TELEGRAM=1 every text event also
    lands in the user's Telegram chat, exactly like cron-originated runs.
    """
    mirror = _make_telegram_mirror(user_id) if _env_flag("MOBILE_REPLY_TELEGRAM", "1") else None

    async def send_func(
        message: str = "",
        voice: bool = False,
        photo_path: str | None = None,
        document_path: str | None = None,
        caption: str = "",
        **_,
    ) -> None:
        text = str(message or "")
        cap = str(caption or "")
        if document_path:
            event_type = "document"
            body = (cap or text).strip() or "document"
        elif photo_path:
            event_type = "image"
            body = (cap or text).strip() or "photo"
        elif voice:
            event_type = "voice"
            body = text
        else:
            event_type = "text"
            body = text.strip()

        if not body and event_type != "voice":
            return

        _enqueue_event(token_id, run_id, event_type, body, speak)
        await _notify_event_waiters()

        if mirror is not None and event_type in ("text", "voice"):
            if body.strip():
                await mirror(body)

    return send_func


async def _execute_mobile_run(token_id: int, run_id: str, user_id: int, text: str, speak: bool) -> None:
    import core

    send_func = build_mobile_send_func(token_id, run_id, user_id, speak)
    lock = _get_run_lock(user_id)

    if lock.locked():
        # The user's new message outranks whatever is running — stop it first,
        # exactly like the web chat handler does.
        try:
            stopped = core.interrupt_user_run(user_id)
            if stopped.get("run_stopped") or stopped.get("agents_stopped"):
                _enqueue_event(
                    token_id, run_id, "status",
                    "Stopping the current task — starting on your new message.", speak,
                )
                await _notify_event_waiters()
        except Exception:
            logger.exception("mobile run interrupt failed")

    async with lock:
        try:
            await core.process_message(user_id, text, send_func)
        except Exception:
            logger.exception("mobile run failed")
            _enqueue_event(token_id, run_id, "error", "Execution failed. Check logs for details.", speak)
            await _notify_event_waiters()
        finally:
            # Sentinel so the app's foreground service knows the run is over.
            _enqueue_event(token_id, run_id, "run_done", "Done.", False)
            await _notify_event_waiters()


# ---------------------------------------------------------------------------
# Bearer-authed handlers (Android app).
# ---------------------------------------------------------------------------

async def handle_status(request):
    env = _read_env()
    version = "unknown"
    try:
        import ota_update

        version = ota_update.current_version()
    except Exception:
        pass
    return web.json_response({
        "ok": True,
        "bot_name": env.get("BOT_NAME", "Clai-TALOS"),
        "telegram_configured": bool(env.get("TELEGRAM_BOT_TOKEN", "").strip()),
        "model_configured": bool(
            env.get("MAIN_MODEL", "").strip()
            or any(env.get(k, "").strip() for k in _PROVIDER_ENV_KEYS)
        ),
        "version": version,
        "glasses_models": GLASSES_MODELS,
        "glasses_model": get_glasses_model(),
        "speak_enabled": _env_flag("MOBILE_SPEAK_ENABLED", "1"),
        "reply_telegram": _env_flag("MOBILE_REPLY_TELEGRAM", "1"),
        "max_message_chars": _env_int("MOBILE_MAX_MESSAGE_CHARS", 4000),
    })


async def handle_message(request):
    token_row = request._mobile_token
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON body"}, status=400)

    text = str(body.get("text", "")).strip()
    if not text:
        return web.json_response({"error": "text is required"}, status=400)

    max_chars = _env_int("MOBILE_MAX_MESSAGE_CHARS", 4000)
    if len(text) > max_chars:
        return web.json_response(
            {"error": f"text too long (max {max_chars} chars)"}, status=413
        )

    origin = str(body.get("origin", "glasses")).strip().lower() or "glasses"
    speak = _env_flag("MOBILE_SPEAK_ENABLED", "1") and origin == "glasses"

    user_id = resolve_primary_user_id()
    run_id = secrets.token_hex(8)

    asyncio.create_task(
        _execute_mobile_run(int(token_row["id"]), run_id, user_id, text, speak)
    )
    security_logger.info(
        "mobile run started: token=%s name=%r user=%d run=%s origin=%s",
        token_row["id"], token_row.get("name"), user_id, run_id, origin,
    )
    return web.json_response({"ok": True, "run_id": run_id, "user_id": user_id}, status=202)


async def handle_events(request):
    token_row = request._mobile_token
    token_id = int(token_row["id"])

    try:
        since = int(str(request.query.get("since", "0")).strip() or "0")
    except ValueError:
        since = 0
    try:
        wait_s = float(str(request.query.get("wait", "25")).strip() or "25")
    except ValueError:
        wait_s = 25.0
    wait_s = max(0.0, min(wait_s, 25.0))

    deadline = time.monotonic() + wait_s
    cond = _events_cond()
    while True:
        events = db.list_mobile_events(token_id, since=since)
        if events:
            last_id = events[-1]["id"]
            return web.json_response({"ok": True, "events": events, "last_id": last_id})
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return web.json_response({"ok": True, "events": [], "last_id": since})
        try:
            async with cond:
                await asyncio.wait_for(cond.wait(), timeout=min(remaining, 5.0))
        except asyncio.TimeoutError:
            continue


async def handle_events_ack(request):
    token_row = request._mobile_token
    try:
        body = await request.json()
    except Exception:
        body = {}
    last_id = body.get("last_id") if isinstance(body, dict) else None
    try:
        last_id = int(last_id)
    except (TypeError, ValueError):
        return web.json_response({"error": "last_id is required"}, status=400)
    count = db.ack_mobile_events(int(token_row["id"]), last_id)
    return web.json_response({"ok": True, "acked": count})


# ---------------------------------------------------------------------------
# Glasses model setting.
# ---------------------------------------------------------------------------

def get_glasses_model() -> str:
    stored = db.get_system_state(_GLASSES_MODEL_STATE_KEY)
    return stored if stored in GLASSES_MODELS else DEFAULT_GLASSES_MODEL


def set_glasses_model(model: str) -> str:
    chosen = str(model or "").strip()
    if chosen not in GLASSES_MODELS:
        raise ValueError("unknown glasses model")
    db.set_system_state(_GLASSES_MODEL_STATE_KEY, chosen)
    return chosen


# ---------------------------------------------------------------------------
# Dashboard-facing handlers (settings card). Auth via passed decorators.
# ---------------------------------------------------------------------------

async def handle_tokens_get(request):
    return web.json_response({
        "ok": True,
        "tokens": db.list_app_tokens(),
        "glasses_models": GLASSES_MODELS,
        "glasses_model": get_glasses_model(),
        "speak_enabled": _env_flag("MOBILE_SPEAK_ENABLED", "1"),
        "reply_telegram": _env_flag("MOBILE_REPLY_TELEGRAM", "1"),
        "mobile_api_enabled": _env_flag("MOBILE_API_ENABLED", "1"),
    })


async def handle_tokens_post(request):
    body = request._json_body if isinstance(request._json_body, dict) else {}
    name = str(body.get("name", "")).strip() or "My glasses"
    if len(name) > 60:
        name = name[:60]

    token = secrets.token_urlsafe(32)
    token_id = db.add_app_token(name, _hash_token(token))
    security_logger.info("mobile app token created: id=%d name=%r", token_id, name)

    payload = json.dumps({
        "v": 1,
        "u": _request_origin(request),
        "t": token,
        "m": get_glasses_model(),
    }, separators=(",", ":"))
    return web.json_response({
        "ok": True,
        "id": token_id,
        "name": name,
        "token": token,
        "qr_payload": payload,
        "qr_url": "/api/qr?scale=8&data=" + quote(payload, safe=""),
        "note": "Save this token now — it is shown only once.",
    })


async def handle_tokens_revoke(request):
    body = request._json_body if isinstance(request._json_body, dict) else {}
    try:
        token_id = int(body.get("id"))
    except (TypeError, ValueError):
        return web.json_response({"error": "id is required"}, status=400)
    ok = db.revoke_app_token(token_id)
    if not ok:
        return web.json_response({"error": "token not found or already revoked"}, status=404)
    security_logger.info("mobile app token revoked: id=%d", token_id)
    return web.json_response({"ok": True})


async def handle_settings_post(request):
    body = request._json_body if isinstance(request._json_body, dict) else {}

    if "glasses_model" in body:
        try:
            set_glasses_model(str(body.get("glasses_model", "")))
        except ValueError:
            return web.json_response(
                {"error": "glasses_model must be one of: " + ", ".join(GLASSES_MODELS)},
                status=400,
            )

    env = None
    for key in ("MOBILE_SPEAK_ENABLED", "MOBILE_REPLY_TELEGRAM"):
        if key in body:
            if env is None:
                env = dict(_read_env())
            env[key] = "1" if str(body[key]).strip().lower() in ("1", "true", "on", "yes") else "0"
    if env is not None:
        _write_env(env)

    return web.json_response({
        "ok": True,
        "glasses_model": get_glasses_model(),
        "speak_enabled": _env_flag("MOBILE_SPEAK_ENABLED", "1"),
        "reply_telegram": _env_flag("MOBILE_REPLY_TELEGRAM", "1"),
    })


# ---------------------------------------------------------------------------
# Wiring.
# ---------------------------------------------------------------------------

def setup_routes(
    app: web.Application,
    *,
    require_auth,
    require_auth_csrf,
    read_env,
    write_env,
    request_origin,
) -> None:
    _deps.update({
        "read_env": read_env,
        "write_env": write_env,
        "request_origin": request_origin,
    })

    app.router.add_get("/api/mobile/status", require_app_token(handle_status))
    app.router.add_post("/api/mobile/message", require_app_token(handle_message))
    app.router.add_get("/api/mobile/events", require_app_token(handle_events))
    app.router.add_post("/api/mobile/events/ack", require_app_token(handle_events_ack))

    app.router.add_get("/api/mobile/tokens", require_auth(handle_tokens_get))
    app.router.add_post("/api/mobile/tokens", require_auth_csrf(handle_tokens_post))
    app.router.add_post("/api/mobile/tokens/revoke", require_auth_csrf(handle_tokens_revoke))
    app.router.add_post("/api/mobile/settings", require_auth_csrf(handle_settings_post))
