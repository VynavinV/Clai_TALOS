import os
import sys
import time
import hmac
import secrets
import logging
import asyncio
import shutil
import subprocess
import json
import base64
import mimetypes
import zlib
import re
import html
from datetime import datetime, timezone
from dotenv import load_dotenv
from aiohttp import web
from telegram.ext import Application
import bcrypt
from bot_handlers import register_handlers, HELP_TEXT
import AI
import db
import cron_jobs
import memory
import gateway
import model_router
import core
import google_integration
import activity_tracker
import app_paths
from auth_policy import MIN_DASHBOARD_PASSWORD_LENGTH, validate_dashboard_password

SCRIPT_DIR = app_paths.resource_root()
SOURCE_DIR = app_paths.source_root()
VENV_DIR = os.path.join(SOURCE_DIR, "venv")
WEB_DIR = app_paths.web_resource_dir()
STATIC_DIR = app_paths.static_resource_dir()
ENV_FILE = app_paths.env_file_path()
CREDS_FILE = app_paths.credentials_file_path()
SECURITY_LOG = app_paths.security_log_path()

if not app_paths.is_frozen() and not hasattr(sys, 'real_prefix') and not (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix):
    print("[warn] Not running in virtual environment. Use start.sh or start.bat.")

app_paths.ensure_runtime_dirs()
_migrated_runtime_items = app_paths.migrate_legacy_runtime_data()
load_dotenv(dotenv_path=ENV_FILE)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
BOT_NAME = os.getenv("BOT_NAME", "Clai-TALOS")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))

MANAGED_KEYS = [
    {"env_key": "ZHIPUAI_API_KEY", "label": "ZhipuAI", "icon": "&#9883;"},
    {"env_key": "GEMINI_API_KEY", "label": "Gemini", "icon": "&#128269;"},
    {"env_key": "OPENAI_API_KEY", "label": "OpenAI", "icon": "&#129302;"},
    {"env_key": "ANTHROPIC_API_KEY", "label": "Anthropic", "icon": "&#129302;"},
    {"env_key": "NVIDIA_API_KEY", "label": "NVIDIA", "icon": "&#9889;"},
    {"env_key": "CEREBRAS_API_KEY", "label": "Cerebras", "icon": "&#9889;"},
    {"env_key": "OPENROUTER_API_KEY", "label": "OpenRouter", "icon": "&#128279;"},
]

SESSION_COOKIE = "talos_session"
SESSION_MAX_AGE = 86400
CSRF_COOKIE = "talos_csrf"
sessions: dict[str, dict] = {}
csrf_tokens: dict[str, float] = {}
login_attempts: dict[str, list] = {}
web_chat_locks: dict[int, asyncio.Lock] = {}
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 300
CSRF_MAX_AGE = 3600
WEB_CHAT_MAX_INLINE_IMAGE_BYTES = 2 * 1024 * 1024
WEB_CHAT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
WEB_UPLOAD_DIR = app_paths.web_upload_dir()
GOOGLE_OAUTH_PENDING_MAX_AGE = 900
google_oauth_pending: dict[str, dict] = {}
start_time = None
_telegram_runtime_app: Application | None = None
_telegram_runtime_token: str = ""
_telegram_runtime_lock: asyncio.Lock | None = None

security_logger = logging.getLogger("talos.security")
security_logger.setLevel(logging.INFO)
handler = logging.FileHandler(SECURITY_LOG)
handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
security_logger.addHandler(handler)

if _migrated_runtime_items:
    print(f"[info] Migrated legacy runtime data to {app_paths.data_root()}: {', '.join(_migrated_runtime_items)}")


def _build_telegram_application(token: str) -> Application:
    app = (
        Application.builder()
        .token(token)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )
    register_handlers(app)
    return app


def _get_telegram_runtime_lock() -> asyncio.Lock:
    global _telegram_runtime_lock
    if _telegram_runtime_lock is None:
        _telegram_runtime_lock = asyncio.Lock()
    return _telegram_runtime_lock


async def _shutdown_telegram_application(app: Application) -> None:
    try:
        if app.updater:
            await app.updater.stop()
    except Exception:
        pass
    try:
        await app.stop()
    except Exception:
        pass
    try:
        await app.shutdown()
    except Exception:
        pass


async def _start_telegram_application(token: str, retries: int = 3) -> tuple[Application | None, str]:
    app = _build_telegram_application(token)
    initialized = False
    started = False

    def _polling_error_callback(exc: Exception) -> None:
        msg = str(exc or "")
        if "terminated by other getUpdates request" in msg:
            print("[warn] Telegram polling conflict detected. Another bot instance is already running with this token.")
            print("[info] Keeping web dashboard online; stop other bot instances to restore Telegram polling here.")
            return
        print(f"[warn] Telegram polling error: {msg}")

    try:
        for attempt in range(1, retries + 1):
            try:
                await app.initialize()
                initialized = True
                break
            except Exception as exc:
                if attempt == retries:
                    return None, f"Failed to initialize Telegram client: {exc}"
                await asyncio.sleep(2)

        await app.start()
        started = True
        await app.updater.start_polling(error_callback=_polling_error_callback)
        return app, ""
    except Exception as exc:
        if started:
            try:
                if app.updater:
                    await app.updater.stop()
            except Exception:
                pass
            try:
                await app.stop()
            except Exception:
                pass
        if initialized:
            try:
                await app.shutdown()
            except Exception:
                pass
        return None, f"Failed to start Telegram polling: {exc}"


async def _start_telegram_runtime(token: str) -> dict:
    global _telegram_runtime_app, _telegram_runtime_token, BOT_TOKEN

    clean_token = str(token or "").strip()
    if not clean_token:
        return {"ok": False, "error": "Telegram token is empty."}

    lock = _get_telegram_runtime_lock()
    async with lock:
        if _telegram_runtime_app is not None and _telegram_runtime_token == clean_token:
            return {"ok": True, "status": "already_running"}

        candidate, err = await _start_telegram_application(clean_token)
        if candidate is None:
            return {"ok": False, "error": err}

        previous = _telegram_runtime_app
        _telegram_runtime_app = candidate
        _telegram_runtime_token = clean_token
        BOT_TOKEN = clean_token

        if previous is not None:
            await _shutdown_telegram_application(previous)
            return {"ok": True, "status": "restarted"}

        return {"ok": True, "status": "started"}


async def _stop_telegram_runtime() -> None:
    global _telegram_runtime_app, _telegram_runtime_token, BOT_TOKEN

    lock = _get_telegram_runtime_lock()
    async with lock:
        current = _telegram_runtime_app
        _telegram_runtime_app = None
        _telegram_runtime_token = ""
        BOT_TOKEN = ""

        if current is not None:
            await _shutdown_telegram_application(current)


def has_credentials() -> bool:
    """Check if admin credentials have been created (not default admin/admin)."""
    if not os.path.isfile(CREDS_FILE):
        return False
    with open(CREDS_FILE, "r") as f:
        content = f.read()
    has_user = any(line.startswith("USERNAME=") for line in content.splitlines())
    has_hash = any(line.startswith("PASSWORD_HASH=") for line in content.splitlines())
    return has_user and has_hash


def needs_onboarding() -> bool:
    """Check if first-time onboarding is needed (no telegram token configured)."""
    env = _read_env_file()
    return not env.get("TELEGRAM_BOT_TOKEN", "").strip()


def _read_env_file() -> dict[str, str]:
    env_vars = {}
    if os.path.isfile(ENV_FILE):
        with open(ENV_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env_vars[k] = v
    return env_vars


def _read_env_int(env_vars: dict[str, str], key: str, default: int) -> int:
    raw = str(env_vars.get(key, "")).strip()
    if not raw:
        return int(default)
    try:
        return int(raw)
    except ValueError:
        return int(default)


def load_credentials():
    if not os.path.isfile(CREDS_FILE):
        return None, None
    username = password_hash = None
    with open(CREDS_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if line.startswith("USERNAME="):
                username = line.split("=", 1)[1]
            elif line.startswith("PASSWORD_HASH="):
                password_hash = line.split("=", 1)[1]
    if username and password_hash:
        return username, password_hash
    username = password = None
    with open(CREDS_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if line.startswith("USERNAME="):
                username = line.split("=", 1)[1]
            elif line.startswith("PASSWORD="):
                password = line.split("=", 1)[1]
    if username and password:
        hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        with open(CREDS_FILE, "r") as f:
            lines = f.readlines()
        with open(CREDS_FILE, "w") as f:
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("PASSWORD="):
                    f.write(f"PASSWORD_HASH={hashed}\n")
                elif stripped.startswith("PASSWORD_HASH="):
                    continue
                else:
                    f.write(line)
        security_logger.info("Migrated plaintext password to bcrypt hash.")
        return username, hashed
    return None, None


def get_client_ip(request):
    peername = request.transport.get_extra_info("peername")
    peer_ip = peername[0] if peername else "unknown"
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded and peer_ip in ("127.0.0.1", "::1", "localhost"):
        return forwarded.split(",")[0].strip()
    return peer_ip


def _is_secure(request):
    if request.scheme == "https":
        return True
    proto = request.headers.get("X-Forwarded-Proto", "")
    return proto.lower() == "https"


def _resolve_tailscale_bin() -> str | None:
    candidates: list[str] = []

    configured = str(os.getenv("TAILSCALE_BIN", "")).strip().strip('"')
    if configured:
        candidates.append(configured)

    for cmd in ("tailscale", "tailscale.exe"):
        found = shutil.which(cmd)
        if found:
            candidates.append(found)

    if sys.platform == "win32":
        program_files = os.getenv("ProgramFiles") or r"C:\Program Files"
        program_files_x86 = os.getenv("ProgramFiles(x86)") or r"C:\Program Files (x86)"
        local_app_data = os.getenv("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        candidates.extend([
            os.path.join(program_files, "Tailscale", "tailscale.exe"),
            os.path.join(program_files_x86, "Tailscale", "tailscale.exe"),
            os.path.join(local_app_data, "Programs", "Tailscale", "tailscale.exe"),
            os.path.join(local_app_data, "Tailscale", "tailscale.exe"),
        ])

    for candidate in candidates:
        candidate = str(candidate or "").strip().strip('"')
        if not candidate:
            continue
        if os.path.isabs(candidate):
            if os.path.isfile(candidate):
                return candidate
            continue
        found = shutil.which(candidate)
        if found:
            return found
    return None


def get_tailscale_ip():
    tailscale_bin = _resolve_tailscale_bin()
    if not tailscale_bin:
        return None
    result = subprocess.run(
        [tailscale_bin, "ip", "-4"],
        capture_output=True, text=True, timeout=5
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return None


def get_tailscale_hostname():
    tailscale_bin = _resolve_tailscale_bin()
    if not tailscale_bin:
        return None
    result = subprocess.run(
        [tailscale_bin, "status", "--json"],
        capture_output=True, text=True, timeout=5
    )
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
        self_info = data.get("Self", {})
        dns_name = self_info.get("DNSName", "").rstrip(".")
        if dns_name:
            return dns_name
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    return None


def is_rate_limited(ip):
    now = time.time()
    attempts = login_attempts.get(ip, [])
    attempts = [t for t in attempts if now - t < LOCKOUT_SECONDS]
    login_attempts[ip] = attempts
    return len(attempts) >= MAX_ATTEMPTS


def record_failed_attempt(ip, username=""):
    login_attempts.setdefault(ip, []).append(time.time())
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    security_logger.warning(f"FAILED_LOGIN ip={ip} username={username} timestamp={ts}")


def log_successful_login(ip, username=""):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    security_logger.info(f"SUCCESSFUL_LOGIN ip={ip} username={username} timestamp={ts}")


def log_logout(ip, username=""):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    security_logger.info(f"LOGOUT ip={ip} username={username} timestamp={ts}")


def create_session(username=""):
    token = secrets.token_urlsafe(48)
    sessions[token] = {"created": time.time(), "username": username}
    return token


def validate_session(token):
    if not token:
        return False
    session = sessions.get(token)
    if not session:
        return False
    if time.time() - session["created"] > SESSION_MAX_AGE:
        del sessions[token]
        return False
    return True


def destroy_session(token):
    sessions.pop(token, None)


def generate_csrf():
    token = secrets.token_urlsafe(32)
    csrf_tokens[token] = time.time()
    return token


def validate_csrf(token):
    if not token or token not in csrf_tokens:
        return False
    if time.time() - csrf_tokens[token] > CSRF_MAX_AGE:
        del csrf_tokens[token]
        return False
    return True


def require_auth_csrf(handler):
    async def middleware(request):
        token = request.cookies.get(SESSION_COOKIE)
        if not validate_session(token):
            return web.Response(
                status=401,
                content_type="application/json",
                text='{"error": "unauthorized"}'
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        csrf = body.get("csrf_token", "") if isinstance(body, dict) else ""
        if not validate_csrf(csrf):
            return web.json_response({"error": "Invalid or expired CSRF token."}, status=403)
        request._json_body = body
        return await handler(request)
    return middleware


def cleanup_csrf():
    now = time.time()
    expired = [t for t, ts in csrf_tokens.items() if now - ts > CSRF_MAX_AGE]
    for t in expired:
        del csrf_tokens[t]


def _cleanup_google_oauth_pending() -> None:
    now = time.time()
    expired = [
        state
        for state, payload in google_oauth_pending.items()
        if now - float(payload.get("created", 0)) > GOOGLE_OAUTH_PENDING_MAX_AGE
    ]
    for state in expired:
        google_oauth_pending.pop(state, None)


def _request_origin(request) -> str:
    proto = request.headers.get("X-Forwarded-Proto", request.scheme).strip() or request.scheme
    host = request.headers.get("X-Forwarded-Host", request.host).strip() or request.host
    return f"{proto}://{host}"


def _google_redirect_uri(request) -> str:
    configured = os.getenv("GOOGLE_OAUTH_REDIRECT_URI", "").strip()
    if configured:
        return configured
    return _request_origin(request) + "/oauth/google/callback"


def check_tailscale():
    tailscale_bin = _resolve_tailscale_bin()
    if not tailscale_bin:
        return False, "not installed"
    result = subprocess.run(
        [tailscale_bin, "status", "--json"],
        capture_output=True, text=True, timeout=5
    )
    if result.returncode != 0:
        return False, "error"
    output = result.stdout
    if '"Online":true' in output or '"Online": true' in output:
        return True, "connected"
    if '"Offline":true' in output or '"Offline": true' in output:
        return False, "offline"
    return False, "unknown"


def check_funnel():
    tailscale_bin = _resolve_tailscale_bin()
    if not tailscale_bin:
        return False, "tailscale not installed"
    result = subprocess.run(
        [tailscale_bin, "funnel", "status"],
        capture_output=True, text=True, timeout=5
    )
    if result.returncode == 0 and str(WEB_PORT) in result.stdout:
        return True, "active"
    result2 = subprocess.run(
        [tailscale_bin, "serve", "status"],
        capture_output=True, text=True, timeout=5
    )
    if result2.returncode == 0 and str(WEB_PORT) in result2.stdout:
        return True, "serving locally"
    return False, "not configured"


def check_venv():
    if sys.platform == "win32":
        pip_path = os.path.join(VENV_DIR, "Scripts", "pip.exe")
    else:
        pip_path = os.path.join(VENV_DIR, "bin", "pip")
    if not os.path.isdir(VENV_DIR):
        return False, "not created"
    if not os.path.isfile(pip_path):
        return False, "incomplete"
    return True, "ready"


def check_credentials():
    if not os.path.isfile(CREDS_FILE):
        return False, "not configured"
    with open(CREDS_FILE, "r") as f:
        content = f.read()
    has_user = any(line.startswith("USERNAME=") for line in content.splitlines())
    has_hash = any(line.startswith("PASSWORD_HASH=") for line in content.splitlines())
    has_plain = any(line.startswith("PASSWORD=") for line in content.splitlines())
    if has_user and (has_hash or has_plain):
        return True, "configured"
    return False, "incomplete"


def render_template(name, **kwargs):
    path = os.path.join(WEB_DIR, name)
    with open(path, "r") as f:
        html_content = f.read()
    for key, value in kwargs.items():
        html_content = html_content.replace("{{" + key + "}}", html.escape(str(value)))
    return html_content


def require_auth(handler):
    async def middleware(request):
        token = request.cookies.get(SESSION_COOKIE)
        if not validate_session(token):
            return web.Response(
                status=401,
                content_type="application/json",
                text='{"error": "unauthorized"}'
            )
        return await handler(request)
    return middleware


def _get_web_chat_lock(user_id: int) -> asyncio.Lock:
    lock = web_chat_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        web_chat_locks[user_id] = lock
    return lock


def _get_web_user_id(request) -> int:
    token = request.cookies.get(SESSION_COOKIE, "")
    session = sessions.get(token, {})
    username = str(session.get("username", "dashboard")).strip().lower() or "dashboard"
    digest = zlib.crc32(username.encode("utf-8")) & 0x7FFFFFFF
    return -(digest or 1)


def _resolve_image_path(path: str) -> str:
    expanded = os.path.expanduser(path)
    if os.path.isabs(expanded):
        return expanded
    data_candidate = os.path.join(app_paths.data_root(), expanded)
    if os.path.exists(data_candidate):
        return data_candidate
    return os.path.join(SCRIPT_DIR, expanded)


def _sanitize_upload_filename(filename: str) -> str:
    raw = os.path.basename(str(filename or "upload.bin").strip())
    if not raw:
        raw = "upload.bin"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", raw)
    return safe[:120] if safe else "upload.bin"


def _decode_data_url(data_url: str) -> tuple[str, bytes]:
    if not isinstance(data_url, str) or not data_url.startswith("data:"):
        raise ValueError("attachment must include a valid data_url")

    if "," not in data_url:
        raise ValueError("invalid data_url format")

    header, encoded = data_url.split(",", 1)
    mime = "application/octet-stream"

    if ";" in header:
        mime = header[5:].split(";", 1)[0] or mime

    if not header.endswith(";base64"):
        raise ValueError("attachment data_url must be base64 encoded")

    try:
        content = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ValueError(f"invalid base64 attachment payload: {exc}")

    return mime, content


def _save_web_upload(attachment: dict) -> dict:
    if not isinstance(attachment, dict):
        return {"error": "attachment must be an object"}

    name = _sanitize_upload_filename(str(attachment.get("name", "upload.bin")))
    data_url = attachment.get("data_url", "")

    try:
        mime, content = _decode_data_url(str(data_url))
    except ValueError as exc:
        return {"error": str(exc)}

    size = len(content)
    if size <= 0:
        return {"error": "attachment is empty"}
    if size > WEB_CHAT_MAX_UPLOAD_BYTES:
        return {
            "error": (
                f"attachment too large ({size} bytes). "
                f"Limit is {WEB_CHAT_MAX_UPLOAD_BYTES} bytes"
            )
        }

    os.makedirs(WEB_UPLOAD_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    token = secrets.token_hex(4)
    stored_name = f"{stamp}_{token}_{name}"
    saved_path = os.path.join(WEB_UPLOAD_DIR, stored_name)

    with open(saved_path, "wb") as f:
        f.write(content)

    relative_path = os.path.relpath(saved_path, app_paths.data_root()).replace("\\", "/")
    return {
        "name": name,
        "path": saved_path,
        "relative_path": relative_path,
        "mime": mime,
        "bytes": size,
        "is_image": mime.startswith("image/"),
    }


def _to_image_event(photo_path: str, caption: str = "", message: str = "") -> dict:
    resolved = _resolve_image_path(str(photo_path or ""))
    event = {
        "type": "image",
        "text": str(caption or message or "").strip(),
        "path": resolved,
    }

    if not os.path.isfile(resolved):
        event["warning"] = "image_not_found"
        return event

    try:
        size = os.path.getsize(resolved)
        event["bytes"] = size
        if size > WEB_CHAT_MAX_INLINE_IMAGE_BYTES:
            event["warning"] = "image_too_large_for_inline_preview"
            return event

        mime, _ = mimetypes.guess_type(resolved)
        mime = mime or "image/png"
        with open(resolved, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("ascii")
        event["data_url"] = f"data:{mime};base64,{encoded}"
        return event
    except Exception:
        event["warning"] = "image_read_failed"
        return event


def _build_web_send_func(events: list[dict]):
    async def send_func(message: str = "", voice: bool = False, photo_path: str | None = None, document_path: str | None = None, caption: str = "") -> None:
        text = str(message or "")
        if document_path:
            resolved = _resolve_image_path(str(document_path or ""))
            events.append({"type": "document", "text": str(caption or message or "").strip(), "path": resolved})
            return
        if photo_path:
            events.append(_to_image_event(str(photo_path), str(caption or ""), text))
            return
        if voice:
            events.append({"type": "voice", "text": text})
            return
        if text.strip():
            events.append({"type": "text", "text": text})

    return send_func


def _handle_web_model_command(user_id: int, text: str) -> list[dict] | None:
    normalized = text.strip()
    if normalized == "/help":
        return [{"type": "text", "text": HELP_TEXT}]
    if normalized == "/model":
        current = db.get_model(user_id)
        models = model_router.list_provider_models()
        return [{
            "type": "text",
            "text": "Current model: " + current + "\nAvailable models:\n- " + "\n- ".join(models),
        }]

    if normalized.startswith("/model "):
        requested = normalized.split(" ", 1)[1].strip()
        models = model_router.list_provider_models()
        if requested not in models:
            return [{"type": "error", "text": f"Unknown model: {requested}"}]
        db.set_model(user_id, requested)
        return [{"type": "text", "text": f"Model set to {requested}"}]

    return None


async def handle_root(request):
    # First-time: no credentials exist -> signup
    if not has_credentials():
        return web.HTTPFound("/signup")

    # Logged in -> dashboard (or onboarding if not set up)
    token = request.cookies.get(SESSION_COOKIE)
    if validate_session(token):
        if needs_onboarding():
            return web.HTTPFound("/onboarding")
        return web.Response(
            text=render_template("dashboard.html", BOT_NAME=BOT_NAME),
            content_type="text/html",
        )

    # Not logged in -> login page
    cleanup_csrf()
    csrf = generate_csrf()
    response = web.Response(
        text=render_template("login.html", BOT_NAME=BOT_NAME, CSRF_TOKEN=csrf),
        content_type="text/html",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=CSRF_MAX_AGE,
        httponly=True,
        secure=_is_secure(request),
        samesite="Strict",
        path="/",
    )
    return response


async def handle_signup(request):
    """First-time signup page - only available when no credentials exist."""
    if has_credentials():
        return web.HTTPFound("/")
    return web.Response(
        text=render_template(
            "signup.html",
            BOT_NAME=BOT_NAME,
            MIN_PASSWORD_LENGTH=MIN_DASHBOARD_PASSWORD_LENGTH,
        ),
        content_type="text/html",
    )


async def handle_api_signup(request):
    """Create admin credentials for the first time."""
    if has_credentials():
        return web.json_response({"error": "Account already exists. Use login."}, status=400)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid request."}, status=400)

    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))

    if not username:
        return web.json_response({"error": "Username is required."}, status=400)

    password_ok, password_error = validate_dashboard_password(password)
    if not password_ok:
        return web.json_response({"error": password_error}, status=400)

    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    with open(CREDS_FILE, "w") as f:
        f.write(f"USERNAME={username}\n")
        f.write(f"PASSWORD_HASH={hashed}\n")
    if sys.platform != "win32":
        os.chmod(CREDS_FILE, 0o600)

    ip = get_client_ip(request)
    security_logger.info(f"ACCOUNT_CREATED ip={ip} username={username}")

    # Auto-login after signup
    session_token = create_session(username)
    response = web.json_response({"ok": True})
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=_is_secure(request),
        samesite="Strict",
        path="/",
    )
    return response


async def handle_onboarding(request):
    """Onboarding wizard - needs auth."""
    token = request.cookies.get(SESSION_COOKIE)
    if not validate_session(token):
        return web.HTTPFound("/")
    return _serve_auth_page(request, "onboarding.html")


@require_auth
async def handle_api_onboarding_telegram(request):
    """Save Telegram token and bot name during onboarding."""
    global BOT_NAME

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid request."}, status=400)

    token = str(body.get("token", "")).strip()
    bot_name = str(body.get("bot_name", "")).strip() or "Clai-TALOS"

    if not token:
        return web.json_response({"error": "Bot token is required."}, status=400)

    env_vars = _read_env_file()
    env_vars["TELEGRAM_BOT_TOKEN"] = token
    env_vars["BOT_NAME"] = bot_name

    with open(ENV_FILE, "w") as f:
        for k, v in env_vars.items():
            f.write(f"{k}={v}\n")

    load_dotenv(dotenv_path=ENV_FILE, override=True)
    BOT_NAME = bot_name

    start_result = await _start_telegram_runtime(token)
    if not start_result.get("ok"):
        detail = str(start_result.get("error", "Unknown startup error"))
        return web.json_response(
            {
                "error": f"Token saved, but Telegram failed to start: {detail}",
            },
            status=400,
        )

    return web.json_response({"ok": True, "telegram": start_result})


@require_auth
async def handle_api_onboarding_tailscale(request):
    """Check Tailscale status for onboarding."""
    installed = bool(_resolve_tailscale_bin())
    connected = False
    hostname = ""

    if installed:
        ts_ok, _ = check_tailscale()
        connected = ts_ok
        if connected:
            hostname = get_tailscale_hostname() or ""

    return web.json_response({
        "installed": installed,
        "connected": connected,
        "hostname": hostname,
    })


@require_auth
async def handle_api_onboarding_model(request):
    """Save model provider, API key, and model selection during onboarding."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid request."}, status=400)

    provider = str(body.get("provider", "")).strip()
    api_key = str(body.get("api_key", "")).strip()
    main_model = str(body.get("main_model", "")).strip()
    image_model = str(body.get("image_model", "")).strip()
    vision_provider = str(body.get("vision_provider", "")).strip()
    vision_api_key = str(body.get("vision_api_key", "")).strip()

    if not provider or not api_key:
        return web.json_response({"error": "Provider and API key are required."}, status=400)

    env_key_map = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "zhipu": "ZHIPUAI_API_KEY",
        "nvidia": "NVIDIA_API_KEY",
        "cerebras": "CEREBRAS_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
    }

    env_key = env_key_map.get(provider)
    if not env_key:
        return web.json_response({"error": "Unknown provider."}, status=400)

    env_vars = _read_env_file()
    env_vars[env_key] = api_key
    if main_model:
        env_vars["MAIN_MODEL"] = main_model
    if image_model:
        env_vars["IMAGE_MODEL"] = image_model

    if vision_provider and vision_api_key:
        vision_env_key = env_key_map.get(vision_provider)
        if vision_env_key:
            env_vars[vision_env_key] = vision_api_key

    with open(ENV_FILE, "w") as f:
        for k, v in env_vars.items():
            f.write(f"{k}={v}\n")

    load_dotenv(dotenv_path=ENV_FILE, override=True)
    AI.reload_clients()

    return web.json_response({"ok": True})


@require_auth
async def handle_api_onboarding_gemini(request):
    """Save Gemini key (for web search) during onboarding."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid request."}, status=400)

    api_key = str(body.get("api_key", "")).strip()
    if not api_key:
        return web.json_response({"ok": True})

    env_vars = _read_env_file()
    env_vars["GEMINI_API_KEY"] = api_key

    with open(ENV_FILE, "w") as f:
        for k, v in env_vars.items():
            f.write(f"{k}={v}\n")

    load_dotenv(dotenv_path=ENV_FILE, override=True)
    return web.json_response({"ok": True})


async def _download_himalaya() -> str | None:
    import platform as _platform
    import tarfile
    import tempfile

    system = _platform.system().lower()
    machine = _platform.machine().lower()

    arch_map = {
        "x86_64": "x86_64", "amd64": "x86_64",
        "aarch64": "aarch64", "arm64": "aarch64",
        "armv7l": "armv7l", "armv6l": "armv6l",
        "i686": "i686",
    }
    os_map = {
        "linux": "linux",
        "darwin": "darwin",
        "windows": "windows",
    }

    arch = arch_map.get(machine)
    os_name = os_map.get(system)

    if not arch or not os_name:
        return None

    asset_name = f"himalaya.{arch}-{os_name}.tgz"
    download_url = f"https://github.com/pimalaya/himalaya/releases/latest/download/{asset_name}"

    bin_dir = app_paths.bin_dir()
    os.makedirs(bin_dir, exist_ok=True)
    bin_path = os.path.join(bin_dir, "himalaya" if os_name != "windows" else "himalaya.exe")

    if os.path.isfile(bin_path):
        try:
            proc = await asyncio.create_subprocess_exec(
                bin_path, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode == 0:
                return bin_path
        except Exception:
            pass

    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-fsSL", "--connect-timeout", "15", "-o", bin_path + ".tgz", download_url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            return None
    except Exception:
        return None

    tgz_path = bin_path + ".tgz"
    if not os.path.isfile(tgz_path) or os.path.getsize(tgz_path) < 1000:
        if os.path.isfile(tgz_path):
            os.remove(tgz_path)
        return None

    try:
        with tarfile.open(tgz_path, "r:gz") as tf:
            for member in tf.getmembers():
                if member.isfile():
                    member.name = os.path.basename(member.name)
                    try:
                        tf.extract(member, bin_dir, filter="data")
                    except TypeError:
                        tf.extract(member, bin_dir)
                    extracted = os.path.join(bin_dir, member.name)
                    os.chmod(extracted, 0o755)
                    if os.path.isfile(extracted):
                        os.rename(extracted, bin_path)
                        break
    except Exception:
        return None
    finally:
        if os.path.isfile(tgz_path):
            os.remove(tgz_path)

    if os.path.isfile(bin_path):
        return bin_path

    return None


@require_auth
async def handle_api_onboarding_email(request):
    """Install Himalaya and configure Gmail account during onboarding."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid request."}, status=400)

    email_addr = str(body.get("email", "")).strip()
    app_password = "".join(str(body.get("app_password", "")).split())

    if not email_addr or not app_password:
        return web.json_response({"error": "Email and app password are required."}, status=400)

    if not email_addr.endswith("@gmail.com"):
        return web.json_response({"error": "Only Gmail accounts are supported for automatic setup."}, status=400)

    himalaya_bin = shutil.which("himalaya")

    if not himalaya_bin:
        himalaya_bin = await _download_himalaya()
        if not himalaya_bin:
            return web.json_response({
                "error": "Failed to download Himalaya. Install manually from github.com/pimalaya/himalaya and set HIMALAYA_BIN in Settings."
            }, status=500)

    config_dir = app_paths.himalaya_dir()
    os.makedirs(config_dir, exist_ok=True)
    config_path = os.path.join(config_dir, "config.toml")
    config_cli_path = os.path.relpath(config_path, app_paths.data_root()).replace("\\", "/")

    username = email_addr.split("@")[0]
    account_alias = "gmail"

    safe_email = email_addr.replace('"', '').replace('\\', '')
    safe_username = username.replace('"', '').replace('\\', '')
    safe_password = app_password.replace('"', '').replace('\\', '')

    config_content = f"""[accounts.{account_alias}]
default = true
email = "{safe_email}"
display-name = "{safe_username}"

folder.aliases.sent = "[Gmail]/Sent Mail"

message.send.save-copy = true

[accounts.{account_alias}.backend]
type = "imap"
host = "imap.gmail.com"
port = 993
encryption.type = "tls"
login = "{safe_email}"
auth.type = "password"
auth.raw = "{safe_password}"

[accounts.{account_alias}.message.send.backend]
type = "smtp"
host = "smtp.gmail.com"
port = 465
encryption.type = "tls"
login = "{safe_email}"
auth.type = "password"
auth.raw = "{safe_password}"
"""

    with open(config_path, "w") as f:
        f.write(config_content)
    os.chmod(config_path, 0o600)

    env_vars = _read_env_file()
    env_vars["HIMALAYA_BIN"] = himalaya_bin
    env_vars["HIMALAYA_CONFIG"] = config_cli_path
    env_vars["HIMALAYA_DEFAULT_ACCOUNT"] = account_alias

    with open(ENV_FILE, "w") as f:
        for k, v in env_vars.items():
            f.write(f"{k}={v}\n")

    load_dotenv(dotenv_path=ENV_FILE, override=True)

    try:
        proc = await asyncio.create_subprocess_exec(
            himalaya_bin, "--config", config_cli_path, "account", "list",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=app_paths.data_root(),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            return web.json_response({
                "error": f"Himalaya config verification failed: {(stderr or stdout or b'').decode(errors='replace')[:300]}"
            }, status=500)
    except Exception as e:
        return web.json_response({"error": f"Himalaya verification error: {e}"}, status=500)

    return web.json_response({"ok": True, "email": email_addr, "config": config_path})


@require_auth
async def handle_api_onboarding_google(request):
    """Save Google API key and OAuth credentials during onboarding."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid request."}, status=400)

    api_key = str(body.get("api_key", "")).strip()
    client_id = str(body.get("oauth_client_id", "")).strip()
    client_secret = str(body.get("oauth_client_secret", "")).strip()
    apps_script_url = str(body.get("apps_script_url", "")).strip()

    if not api_key and not client_id:
        return web.json_response({"ok": True})

    env_vars = _read_env_file()
    if api_key:
        env_vars["GOOGLE_API_KEY"] = api_key
    if client_id:
        env_vars["GOOGLE_OAUTH_CLIENT_ID"] = client_id
    if client_secret:
        env_vars["GOOGLE_OAUTH_CLIENT_SECRET"] = client_secret
    if apps_script_url:
        env_vars["GOOGLE_APPS_SCRIPT_URL"] = apps_script_url

    with open(ENV_FILE, "w") as f:
        for k, v in env_vars.items():
            f.write(f"{k}={v}\n")

    load_dotenv(dotenv_path=ENV_FILE, override=True)
    return web.json_response({"ok": True})


def _serve_auth_page(request, template_name: str):
    """Serve an authenticated HTML page with a fresh CSRF token."""
    csrf = generate_csrf()
    response = web.Response(
        text=render_template(template_name, BOT_NAME=BOT_NAME, CSRF_TOKEN=csrf),
        content_type="text/html",
    )
    response.set_cookie(
        CSRF_COOKIE, csrf, max_age=CSRF_MAX_AGE,
        httponly=True, secure=_is_secure(request), samesite="Strict", path="/",
    )
    return response


async def handle_dashboard(request):
    token = request.cookies.get(SESSION_COOKIE)
    if not validate_session(token):
        return web.HTTPFound("/")
    if needs_onboarding():
        return web.HTTPFound("/onboarding")
    return _serve_auth_page(request, "dashboard.html")


async def handle_activity(request):
    token = request.cookies.get(SESSION_COOKIE)
    if not validate_session(token):
        return web.HTTPFound("/")
    if needs_onboarding():
        return web.HTTPFound("/onboarding")
    return _serve_auth_page(request, "activity.html")


async def handle_api_activity_history(request):
    token = request.cookies.get(SESSION_COOKIE)
    if not validate_session(token):
        return web.json_response({"error": "unauthorized"}, status=401)
    tracker = activity_tracker.get_tracker()
    return web.json_response(tracker.get_recent(200))


async def handle_api_activity_stream(request):
    token = request.cookies.get(SESSION_COOKIE)
    if not validate_session(token):
        return web.Response(status=401, text="unauthorized")
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await resp.prepare(request)
    tracker = activity_tracker.get_tracker()
    queue = await tracker.subscribe()
    try:
        while True:
            try:
                evt = await asyncio.wait_for(queue.get(), timeout=30)
                data = json.dumps(evt, separators=(",", ":"))
                await resp.write(f"data: {data}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        await tracker.unsubscribe(queue)
    return resp


async def handle_keys(request):
    token = request.cookies.get(SESSION_COOKIE)
    if not validate_session(token):
        return web.HTTPFound("/")
    return _serve_auth_page(request, "keys.html")


def _read_env_keys():
    env_vars = _read_env_file()
    keys = []
    for mk in MANAGED_KEYS:
        keys.append({
            "env_key": mk["env_key"],
            "label": mk["label"],
            "icon": mk["icon"],
            "is_set": bool(env_vars.get(mk["env_key"], "").strip()),
        })
    return keys


@require_auth
async def handle_api_keys_get(request):
    return web.json_response(_read_env_keys())


@require_auth_csrf
async def handle_api_keys_post(request):
    body = request._json_body

    env_vars = _read_env_file()

    for mk in MANAGED_KEYS:
        ek = mk["env_key"]
        if ek in body and body[ek].strip():
            val = body[ek].strip()
            if "****" in val and len(val) < 20:
                continue
            env_vars[ek] = val

    with open(ENV_FILE, "w") as f:
        for k, v in env_vars.items():
            f.write(f"{k}={v}\n")

    load_dotenv(dotenv_path=ENV_FILE, override=True)
    AI.reload_clients()
    import websearch
    websearch.reload_client()

    return web.json_response({"ok": True, "keys": _read_env_keys()})


async def handle_login(request):
    ip = get_client_ip(request)

    if is_rate_limited(ip):
        security_logger.warning(f"RATE_LIMITED ip={ip}")
        return web.json_response(
            {"error": "Too many failed attempts. Try again later."},
            status=429
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid request."}, status=400)

    csrf_token = body.get("csrf_token", "")
    if not validate_csrf(csrf_token):
        security_logger.warning(f"CSRF_FAILURE ip={ip}")
        return web.json_response({"error": "Invalid or expired session. Refresh the page."}, status=403)

    username = body.get("username", "")
    password = body.get("password", "")

    if not username or not password:
        return web.json_response({"error": "Username and password required."}, status=400)

    stored_user, stored_hash = load_credentials()

    if not stored_user or not stored_hash:
        return web.json_response({"error": "No account exists yet. Go to /signup first."}, status=400)

    user_match = hmac.compare_digest(username, stored_user)
    pass_match = False
    if user_match:
        try:
            pass_match = bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8"))
        except Exception:
            pass_match = False

    if not user_match or not pass_match:
        record_failed_attempt(ip, username)
        return web.json_response({"error": "Invalid username or password."}, status=401)

    login_attempts.pop(ip, None)
    log_successful_login(ip, username)
    session_token = create_session(username)
    response = web.json_response({"ok": True})
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=_is_secure(request),
        samesite="Strict",
        path="/",
    )
    response.set_cookie(CSRF_COOKIE, "", max_age=0, path="/")
    return response


@require_auth
async def handle_logout(request):
    token = request.cookies.get(SESSION_COOKIE)
    ip = get_client_ip(request)
    username = ""
    if token and token in sessions:
        username = sessions[token].get("username", "")
    if token:
        destroy_session(token)
    log_logout(ip, username)
    response = web.json_response({"ok": True})
    response.set_cookie(
        SESSION_COOKIE,
        "",
        max_age=0,
        httponly=True,
        secure=_is_secure(request),
        samesite="Strict",
        path="/",
    )
    return response


@require_auth
async def handle_status(request):
    ts_ok, ts_detail = check_tailscale()
    fn_ok, fn_detail = check_funnel()
    venv_ok, venv_detail = check_venv()
    creds_ok, creds_detail = check_credentials()

    uptime = ""
    if start_time:
        elapsed = int(time.time() - start_time)
        h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
        uptime = f"{h:02d}:{m:02d}:{s:02d}"

    bot_online = _telegram_runtime_app is not None
    bot_detail = "online" if bot_online else "web-only"

    return web.json_response({
        "bot": {"ok": bot_online, "detail": bot_detail},
        "tailscale": {"ok": ts_ok, "detail": ts_detail},
        "funnel": {"ok": fn_ok, "detail": fn_detail},
        "venv": {"ok": venv_ok, "detail": venv_detail},
        "credentials": {"ok": creds_ok, "detail": creds_detail},
        "uptime": uptime,
    })


@require_auth
async def handle_settings(request):
    return _serve_auth_page(request, "settings.html")


@require_auth
async def handle_tools(request):
    return _serve_auth_page(request, "tools.html")


@require_auth
async def handle_projects_page(request):
    return _serve_auth_page(request, "projects.html")


_SECRET_KEYS = frozenset({
    "TELEGRAM_BOT_TOKEN", "ZHIPUAI_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY", "NVIDIA_API_KEY", "CEREBRAS_API_KEY", "OPENROUTER_API_KEY",
    "GOOGLE_API_KEY", "GOOGLE_OAUTH_CLIENT_SECRET",
})


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return value[:4] + "****" + value[-4:]


@require_auth
async def handle_api_settings_get(request):
    env_vars = _read_env_file()
    result = {
        "BOT_NAME": env_vars.get("BOT_NAME", ""),
        "WEB_PORT": env_vars.get("WEB_PORT", "8080"),
        "MAIN_MODEL": env_vars.get("MAIN_MODEL", ""),
        "IMAGE_MODEL": env_vars.get("IMAGE_MODEL", ""),
        "FAST_MODEL": env_vars.get("FAST_MODEL", ""),
        "GOOGLE_OAUTH_CLIENT_ID": env_vars.get("GOOGLE_OAUTH_CLIENT_ID", ""),
        "GOOGLE_OAUTH_REDIRECT_URI": env_vars.get("GOOGLE_OAUTH_REDIRECT_URI", ""),
        "GOOGLE_APPS_SCRIPT_URL": env_vars.get("GOOGLE_APPS_SCRIPT_URL", ""),
        "GOOGLE_OAUTH_SCOPES": env_vars.get("GOOGLE_OAUTH_SCOPES", ""),
        "HIMALAYA_BIN": env_vars.get("HIMALAYA_BIN", ""),
        "HIMALAYA_CONFIG": env_vars.get("HIMALAYA_CONFIG", ""),
        "HIMALAYA_DEFAULT_ACCOUNT": env_vars.get("HIMALAYA_DEFAULT_ACCOUNT", ""),
        "PIPER_VOICE": env_vars.get("PIPER_VOICE", "en_US-lessac-medium"),
        "CLIENT_BASE_URL": env_vars.get("CLIENT_BASE_URL", "https://api.z.ai/api/coding/paas/v4"),
        "NVIDIA_BASE_URL": env_vars.get("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        "CEREBRAS_BASE_URL": env_vars.get("CEREBRAS_BASE_URL", "https://api.cerebras.ai/v1"),
        "OPENROUTER_BASE_URL": env_vars.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        "OLLAMA_BASE_URL": env_vars.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        "OLLAMA_MODEL": env_vars.get("OLLAMA_MODEL", ""),
        "MAX_TOOL_ROUNDS": env_vars.get("MAX_TOOL_ROUNDS", "5"),
        "MAX_TOOL_CALLS_PER_ROUND": env_vars.get("MAX_TOOL_CALLS_PER_ROUND", "20"),
        "MAX_COMMAND_TIMEOUT": env_vars.get("MAX_COMMAND_TIMEOUT", "120"),
        "MAX_WORKFLOW_STEPS": env_vars.get("MAX_WORKFLOW_STEPS", "12"),
        "MAX_SUBAGENT_TOOL_ROUNDS": env_vars.get("MAX_SUBAGENT_TOOL_ROUNDS", "5"),
        "MAX_SUBAGENT_TOOL_CALLS_PER_ROUND": env_vars.get("MAX_SUBAGENT_TOOL_CALLS_PER_ROUND", "15"),
        "MAX_CONTEXT_CHARS": env_vars.get("MAX_CONTEXT_CHARS", "120000"),
    }
    for key in _SECRET_KEYS:
        result[key] = _mask_secret(env_vars.get(key, ""))
    return web.json_response(result)


@require_auth
async def handle_api_context_usage(request):
    env_vars = _read_env_file()
    threshold = max(10000, _read_env_int(env_vars, "MAX_CONTEXT_CHARS", 120000))

    user_id = _get_web_user_id(request)
    history_limit = max(db.HISTORY_WINDOW * 3, 80)
    history = db.get_history(user_id, limit=history_limit)

    system = AI._build_system(user_id, "", include_memories=False)
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.extend(history)

    if hasattr(AI, "_estimate_messages_chars"):
        used_chars = int(AI._estimate_messages_chars(messages))
    else:
        used_chars = sum(len(str(m.get("content", ""))) + len(str(m.get("role", ""))) for m in messages)

    percent = 0.0
    if threshold > 0:
        percent = round((used_chars / threshold) * 100.0, 1)

    state = "safe"
    if percent >= 90:
        state = "critical"
    elif percent >= 70:
        state = "warning"

    return web.json_response({
        "ok": True,
        "threshold_chars": threshold,
        "used_chars": used_chars,
        "remaining_chars": max(0, threshold - used_chars),
        "percent": percent,
        "state": state,
        "history_messages": len(history),
        "summary_present": bool(db.get_summary(user_id)),
    })


@require_auth_csrf
async def handle_api_settings_post(request):
    global BOT_NAME

    body = request._json_body

    env_vars = _read_env_file()

    _TEXT_KEYS = [
        "BOT_NAME", "TELEGRAM_BOT_TOKEN", "WEB_PORT",
        "ZHIPUAI_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "NVIDIA_API_KEY", "CEREBRAS_API_KEY", "OPENROUTER_API_KEY",
        "GOOGLE_API_KEY", "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
        "GOOGLE_OAUTH_REDIRECT_URI", "GOOGLE_APPS_SCRIPT_URL", "GOOGLE_OAUTH_SCOPES",
        "HIMALAYA_BIN", "HIMALAYA_CONFIG", "HIMALAYA_DEFAULT_ACCOUNT",
        "PIPER_VOICE", "CLIENT_BASE_URL", "NVIDIA_BASE_URL", "CEREBRAS_BASE_URL", "OPENROUTER_BASE_URL", "OLLAMA_BASE_URL", "OLLAMA_MODEL",
    ]

    def _is_masked(val: str) -> bool:
        return "****" in val and len(val) < 20

    _MODEL_KEYS = ["MAIN_MODEL", "IMAGE_MODEL", "FAST_MODEL"]

    for key in _MODEL_KEYS:
        if key in body:
            value = str(body[key]).strip()
            if value:
                env_vars[key] = value
            elif key in env_vars:
                del env_vars[key]

    for key in _TEXT_KEYS:
        if key in body and body[key].strip():
            if key in _SECRET_KEYS and _is_masked(body[key].strip()):
                continue
            env_vars[key] = body[key].strip()

    _INT_KEYS = [
        ("MAX_TOOL_ROUNDS", 1, 50),
        ("MAX_TOOL_CALLS_PER_ROUND", 1, 100),
        ("MAX_COMMAND_TIMEOUT", 5, 600),
        ("MAX_WORKFLOW_STEPS", 1, 50),
        ("MAX_SUBAGENT_TOOL_ROUNDS", 1, 50),
        ("MAX_SUBAGENT_TOOL_CALLS_PER_ROUND", 1, 100),
        ("MAX_CONTEXT_CHARS", 10000, 1000000),
    ]

    for key, lo, hi in _INT_KEYS:
        if key in body:
            raw = str(body[key]).strip()
            if raw:
                try:
                    val = int(raw)
                    if val < lo or val > hi:
                        return web.json_response(
                            {"error": f"{key} must be between {lo} and {hi}."},
                            status=400,
                        )
                    env_vars[key] = str(val)
                except ValueError:
                    return web.json_response(
                        {"error": f"{key} must be a valid integer."},
                        status=400,
                    )

    with open(ENV_FILE, "w") as f:
        for k, v in env_vars.items():
            f.write(f"{k}={v}\n")

    load_dotenv(dotenv_path=ENV_FILE, override=True)
    AI.reload_clients()

    new_bot_name = str(env_vars.get("BOT_NAME", "")).strip()
    if new_bot_name:
        BOT_NAME = new_bot_name

    telegram_token = str(env_vars.get("TELEGRAM_BOT_TOKEN", "")).strip()
    if telegram_token:
        start_result = await _start_telegram_runtime(telegram_token)
        if not start_result.get("ok"):
            detail = str(start_result.get("error", "Unknown startup error"))
            return web.json_response(
                {
                    "error": f"Settings were saved, but Telegram failed to start: {detail}",
                },
                status=502,
            )
        return web.json_response({"ok": True, "telegram": start_result})

    await _stop_telegram_runtime()

    return web.json_response({"ok": True})


@require_auth
async def handle_api_models(request):
    return web.json_response({
        "main_models": model_router.list_provider_models(),
        "image_models": model_router.list_image_models(),
        "all_models": model_router.list_models_with_provider(),
    })


@require_auth
async def handle_api_models_fetch(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid request."}, status=400)
    provider = str(body.get("provider", "")).strip()
    api_key = str(body.get("api_key", "")).strip()
    if not provider:
        return web.json_response({"error": "Provider is required."}, status=400)
    if provider != "ollama" and not api_key:
        return web.json_response({"error": "Provider and API key are required."}, status=400)
    result = model_router.fetch_provider_models(provider, api_key or "ollama")
    return web.json_response(result)


@require_auth_csrf
async def handle_api_ollama_setup(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid request."}, status=400)

    model_name = str(body.get("model_name", "")).strip()
    if not model_name:
        return web.json_response({"error": "Model name is required."}, status=400)

    import shutil
    import subprocess

    ollama_bin = shutil.which("ollama")

    if not ollama_bin:
        return web.json_response({"error": "Ollama is not installed. Install it from https://ollama.com then restart."}, status=400)

    try:
        check = subprocess.run(
            [ollama_bin, "list"],
            capture_output=True, text=True, timeout=10,
        )
        if check.returncode != 0:
            return web.json_response({"error": "Ollama does not appear to be running. Start it first."}, status=400)
    except Exception:
        return web.json_response({"error": "Could not check Ollama status. Is it running?"}, status=400)

    env_vars = _read_env_file()
    env_vars["OLLAMA_MODEL"] = model_name

    with open(ENV_FILE, "w") as f:
        for k, v in env_vars.items():
            f.write(f"{k}={v}\n")

    load_dotenv(dotenv_path=ENV_FILE, override=True)
    model_router.reload_clients()

    pull_result = subprocess.run(
        [ollama_bin, "pull", model_name],
        capture_output=True, text=True, timeout=300,
    )

    if pull_result.returncode != 0:
        return web.json_response({
            "ok": False,
            "error": f"Model saved but pull failed: {pull_result.stderr.strip()}",
            "stdout": pull_result.stdout,
            "stderr": pull_result.stderr,
        })

    return web.json_response({
        "ok": True,
        "model": model_name,
        "output": pull_result.stdout.strip(),
    })


@require_auth
async def handle_api_google_status(request):
    status = google_integration.get_status()
    status["redirect_uri"] = _google_redirect_uri(request)
    return web.json_response(status)


@require_auth
async def handle_api_google_connect(request):
    _cleanup_google_oauth_pending()
    redirect_uri = _google_redirect_uri(request)
    flow = google_integration.start_oauth_flow(redirect_uri=redirect_uri)
    if "error" in flow:
        return web.json_response(flow, status=400)

    state = flow["state"]
    session_token = request.cookies.get(SESSION_COOKIE, "")
    google_oauth_pending[state] = {
        "created": time.time(),
        "code_verifier": flow["code_verifier"],
        "redirect_uri": redirect_uri,
        "session_token": session_token,
    }
    return web.json_response({
        "ok": True,
        "auth_url": flow["auth_url"],
        "redirect_uri": redirect_uri,
        "scopes": flow.get("scopes", []),
    })


async def handle_google_oauth_callback(request):
    _cleanup_google_oauth_pending()

    error = str(request.query.get("error", "")).strip()
    if error:
        html = (
            "<html><body><h2>Google connection failed</h2>"
            f"<p>Error: {error}</p>"
            "<p>Go back to Settings and try again.</p></body></html>"
        )
        return web.Response(text=html, content_type="text/html", status=400)

    code = str(request.query.get("code", "")).strip()
    state = str(request.query.get("state", "")).strip()
    pending = google_oauth_pending.pop(state, None)

    if not code or not state or not pending:
        html = (
            "<html><body><h2>Google connection failed</h2>"
            "<p>Invalid or expired OAuth state.</p>"
            "<p>Go back to Settings and click Connect Google again.</p></body></html>"
        )
        return web.Response(text=html, content_type="text/html", status=400)

    expected_session = str(pending.get("session_token", ""))
    current_session = str(request.cookies.get(SESSION_COOKIE, ""))
    if expected_session and current_session and expected_session != current_session:
        html = (
            "<html><body><h2>Google connection failed</h2>"
            "<p>Session mismatch detected. Please retry from your current dashboard session.</p></body></html>"
        )
        return web.Response(text=html, content_type="text/html", status=403)

    result = await google_integration.exchange_code_for_tokens(
        code=code,
        code_verifier=str(pending.get("code_verifier", "")),
        redirect_uri=str(pending.get("redirect_uri", "")),
    )
    if "error" in result:
        detail = str(result.get("detail", result.get("error", "Unknown error")))
        html = (
            "<html><body><h2>Google connection failed</h2>"
            f"<p>{detail}</p>"
            "<p>Check your OAuth Client ID, secret, and redirect URI in Settings.</p></body></html>"
        )
        return web.Response(text=html, content_type="text/html", status=400)

    html = (
        "<html><body><h2>Google connected</h2>"
        "<p>You can close this tab and return to TALOS Settings.</p>"
        "<script>setTimeout(function(){ window.close(); }, 1200);</script>"
        "</body></html>"
    )
    return web.Response(text=html, content_type="text/html")


@require_auth
async def handle_api_google_disconnect(request):
    return web.json_response(google_integration.disconnect())


@require_auth
async def handle_api_google_test(request):
    result = await google_integration.test_connection()
    if "error" in result:
        return web.json_response(result, status=400)

    status = google_integration.get_status()
    response = {
        "ok": True,
        "google": result,
        "status": status,
    }

    if status.get("has_apps_script_url"):
        ping = await google_integration.execute_apps_script("__ping__", {"source": "dashboard_test"})
        response["apps_script"] = ping
        if "error" in ping:
            response["warning"] = "Google linked, but Apps Script ping failed."

    return web.json_response(response)


@require_auth_csrf
async def handle_api_chat(request):
    body = request._json_body

    text = str(body.get("message", "")).strip()
    attachment = body.get("attachment")
    if not text and not attachment:
        logging.warning(f"handle_api_chat: empty message, body keys={list(body.keys())}")
        return web.json_response({"error": "message or attachment is required"}, status=400)

    user_id = _get_web_user_id(request)
    command_events = _handle_web_model_command(user_id, text)
    if command_events is not None and not attachment:
        return web.json_response({"ok": True, "events": command_events})

    events: list[dict] = []
    upload_info = None
    if attachment:
        upload_info = _save_web_upload(attachment)
        if "error" in upload_info:
            return web.json_response({"error": upload_info["error"]}, status=400)

        if upload_info.get("is_image"):
            events.append(_to_image_event(upload_info["path"], caption=f"Attached file: {upload_info['name']}"))
        else:
            events.append({
                "type": "text",
                "text": f"Attached file: {upload_info['name']} ({upload_info['relative_path']})",
            })

        upload_note = (
            "Uploaded file details:\n"
            f"- name: {upload_info['name']}\n"
            f"- path: {upload_info['relative_path']}\n"
            f"- mime: {upload_info['mime']}\n"
            f"- bytes: {upload_info['bytes']}\n"
            "Use tools like read_file or browser tools on this path when relevant."
        )
        text = (text + "\n\n" + upload_note).strip() if text else upload_note

    send_func = _build_web_send_func(events)
    lock = _get_web_chat_lock(user_id)

    async with lock:
        try:
            await core.process_message(user_id, text, send_func)
        except Exception as e:
            security_logger.exception("Web chat execution failed")
            events.append({"type": "error", "text": "Execution failed. Check logs for details."})

    return web.json_response({"ok": True, "events": events})


@require_auth
async def handle_api_tools_get(request):
    config_file = app_paths.tools_config_path()
    enabled_tools = {}

    if os.path.isfile(config_file):
        try:
            with open(config_file, "r") as f:
                enabled_tools = json.load(f)
        except Exception:
            pass

    all_tools = {
        "execute_command": True,
        "execute_workflow": True,
        "schedule_cron": True,
        "list_cron": True,
        "remove_cron": True,
        "save_memory": True,
        "search_memories": True,
        "list_memories": True,
        "delete_memory": True,
        "update_memory": True,
        "set_model_prefs": True,
        "web_search": True,
        "scrape_url": True,
        "google_execute": True,
        "email_execute": True,
        "browser_start_chrome_debug": True,
        "browser_connect": True,
        "browser_run": True,
        "browser_state": True,
        "browser_disconnect": True,
        "read_file": True,
        "write_file": True,
        "edit_file": True,
        "spreadsheet_execute": True,
        "docx_execute": True,
        "create_tool": True,
        "list_dynamic_tools": True,
        "delete_tool": True,
        "spawn_subagent": True,
        "send_telegram_message": True,
        "send_voice_message": True,
        "send_telegram_photo": True,
        "send_telegram_screenshot": True,
        "create_project": True,
        "list_projects": True,
    }

    for tool_id in all_tools:
        if tool_id not in enabled_tools:
            enabled_tools[tool_id] = True

    return web.json_response(enabled_tools)


@require_auth_csrf
async def handle_api_tools_post(request):
    body = request._json_body

    config_file = app_paths.tools_config_path()

    with open(config_file, "w") as f:
        json.dump(body, f, indent=2)

    return web.json_response({"ok": True})


@require_auth_csrf
async def handle_api_reload(request):
    try:
        load_dotenv(dotenv_path=ENV_FILE, override=True)
        AI.reload_clients()
        import websearch
        websearch.reload_client()
        return web.json_response({"ok": True})
    except Exception:
        return web.json_response({"error": "Reload failed."}, status=500)


@require_auth_csrf
async def handle_api_restart(request):
    try:
        if app_paths.is_frozen():
            subprocess.Popen([sys.executable])
            sys.exit(0)
        target_script = os.path.join(SOURCE_DIR, "telegram_bot.py")
        if sys.platform == "win32":
            subprocess.Popen([sys.executable, target_script])
            sys.exit(0)
        else:
            os.execv(sys.executable, [sys.executable, target_script])
        return web.json_response({"ok": True})
    except Exception:
        return web.json_response({"error": "Restart failed."}, status=500)


async def main():
    global start_time
    start_time = time.time()

    cron_stop = asyncio.Event()

    db.init()
    memory.init()
    gateway.init()
    load_credentials()

    @web.middleware
    async def security_headers_middleware(request, handler):
        response = await handler(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; font-src 'self'"
        )
        return response

    web_app = web.Application(
        client_max_size=12 * 1024 * 1024,
        middlewares=[security_headers_middleware],
    )
    web_app.router.add_get("/", handle_root)
    web_app.router.add_get("/signup", handle_signup)
    web_app.router.add_post("/api/signup", handle_api_signup)
    web_app.router.add_get("/onboarding", handle_onboarding)
    web_app.router.add_post("/api/onboarding/telegram", handle_api_onboarding_telegram)
    web_app.router.add_get("/api/onboarding/tailscale", handle_api_onboarding_tailscale)
    web_app.router.add_post("/api/onboarding/model", handle_api_onboarding_model)
    web_app.router.add_post("/api/onboarding/gemini", handle_api_onboarding_gemini)
    web_app.router.add_post("/api/onboarding/email", handle_api_onboarding_email)
    web_app.router.add_post("/api/onboarding/google", handle_api_onboarding_google)
    web_app.router.add_get("/dashboard", handle_dashboard)
    web_app.router.add_get("/activity", handle_activity)
    web_app.router.add_get("/keys", handle_keys)
    web_app.router.add_get("/settings", handle_settings)
    web_app.router.add_get("/tools", handle_tools)
    web_app.router.add_get("/projects", handle_projects_page)
    web_app.router.add_post("/login", handle_login)
    web_app.router.add_post("/logout", handle_logout)
    web_app.router.add_get("/api/status", handle_status)
    web_app.router.add_get("/api/keys", handle_api_keys_get)
    web_app.router.add_post("/api/keys", handle_api_keys_post)
    web_app.router.add_get("/api/settings", handle_api_settings_get)
    web_app.router.add_post("/api/settings", handle_api_settings_post)
    web_app.router.add_get("/api/context-usage", handle_api_context_usage)
    web_app.router.add_get("/api/google/status", handle_api_google_status)
    web_app.router.add_post("/api/google/connect", handle_api_google_connect)
    web_app.router.add_get("/oauth/google/callback", handle_google_oauth_callback)
    web_app.router.add_post("/api/google/disconnect", handle_api_google_disconnect)
    web_app.router.add_post("/api/google/test", handle_api_google_test)
    web_app.router.add_get("/api/tools", handle_api_tools_get)
    web_app.router.add_post("/api/tools", handle_api_tools_post)
    web_app.router.add_get("/api/models", handle_api_models)
    web_app.router.add_post("/api/models/fetch", handle_api_models_fetch)
    web_app.router.add_post("/api/ollama/setup", handle_api_ollama_setup)
    web_app.router.add_post("/api/chat", handle_api_chat)
    web_app.router.add_get("/api/activity/history", handle_api_activity_history)
    web_app.router.add_get("/api/activity/stream", handle_api_activity_stream)
    web_app.router.add_post("/api/reload", handle_api_reload)
    web_app.router.add_post("/api/restart", handle_api_restart)
    web_app.router.add_static("/static", STATIC_DIR)
    gateway.setup_routes(web_app, auth_decorator=require_auth)

    runner = web.AppRunner(web_app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    try:
        await site.start()
    except OSError as exc:
        err_no = getattr(exc, "errno", None)
        if err_no == 10048 or "10048" in str(exc):
            print(f"[fail] Port {WEB_PORT} is already in use.")
            print("[info] Another TALOS instance is likely already running.")
            print(f"[info] Open http://localhost:{WEB_PORT} or stop the other instance and restart.")
            await runner.cleanup()
            return
        raise

    ts_ip = get_tailscale_ip()
    ts_hostname = get_tailscale_hostname()
    fn_ok, _ = check_funnel()
    print(f"Web dashboard: http://localhost:{WEB_PORT}")
    if ts_hostname and fn_ok:
        print(f"Public URL:    https://{ts_hostname}")
    elif ts_hostname:
        print(f"Tailscale:     http://{ts_hostname}:{WEB_PORT}")
    elif ts_ip:
        print(f"Tailscale:     http://{ts_ip}:{WEB_PORT}")

    print("Running. Press Ctrl+C to stop.")

    if BOT_TOKEN:
        startup = await _start_telegram_runtime(BOT_TOKEN)
        if startup.get("ok"):
            print("Telegram bot online.")
        else:
            print(f"Failed to connect to Telegram on startup: {startup.get('error', 'unknown error')}")
            print("Continuing in web-only mode. Set a valid token in onboarding/settings to auto-start Telegram.")
    else:
        print("No Telegram token configured. Running web-only mode.")
        print("Complete onboarding at the dashboard to connect Telegram.")

    cron_task = asyncio.create_task(cron_jobs.cron_loop(cron_stop))

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        cron_stop.set()
        cron_task.cancel()
        await _stop_telegram_runtime()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
