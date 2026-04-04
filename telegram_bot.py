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
from datetime import datetime, timezone
from dotenv import load_dotenv
from aiohttp import web
from telegram.ext import Application
import bcrypt
from bot_handlers import register_handlers
import AI
import db
import cron_jobs
import memory
import gateway

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.join(SCRIPT_DIR, "venv")
WEB_DIR = os.path.join(SCRIPT_DIR, "web")
STATIC_DIR = os.path.join(WEB_DIR, "static")
ENV_FILE = os.path.join(SCRIPT_DIR, ".env")
CREDS_FILE = os.path.join(SCRIPT_DIR, ".credentials")
SECURITY_LOG = os.path.join(SCRIPT_DIR, ".security.log")

if not hasattr(sys, 'real_prefix') and not (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix):
    print("\n" + "="*50)
    print("ERROR: Not running in virtual environment")
    print("="*50)
    if sys.platform == "win32":
        print("\nPlease use the start script instead:")
        print("  start.bat")
        print("\nOr manually activate venv:")
        print("  venv\\Scripts\\activate")
        print("  python telegram_bot.py")
    else:
        print("\nPlease use the start script instead:")
        print("  ./start.sh")
        print("\nOr manually activate venv:")
        print("  source venv/bin/activate")
        print("  python3 telegram_bot.py")
    print("="*50 + "\n")
    sys.exit(1)

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
BOT_NAME = os.getenv("BOT_NAME", "TALOS")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))

if not BOT_TOKEN:
    print("Error: TELEGRAM_BOT_TOKEN environment variable not set.")
    print("1. Message @BotFather on Telegram and create a bot")
    print("2. Copy the token it gives you")
    print("3. Create a .env file with: TELEGRAM_BOT_TOKEN=your_token_here")
    sys.exit(1)

MANAGED_KEYS = [
    {"env_key": "ZHIPUAI_API_KEY", "label": "ZhipuAI", "icon": "&#9883;"},
    {"env_key": "GEMINI_API_KEY", "label": "Gemini", "icon": "&#128269;"},
    {"env_key": "OPENAI_API_KEY", "label": "OpenAI", "icon": "&#129302;"},
    {"env_key": "ANTHROPIC_API_KEY", "label": "Anthropic", "icon": "&#129302;"},
    {"env_key": "FIRECRAWL_API_KEY", "label": "Firecrawl", "icon": "&#128293;"},
]

SESSION_COOKIE = "talos_session"
SESSION_MAX_AGE = 86400
CSRF_COOKIE = "talos_csrf"
sessions: dict[str, dict] = {}
csrf_tokens: dict[str, float] = {}
login_attempts: dict[str, list] = {}
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 300
CSRF_MAX_AGE = 3600
start_time = None

security_logger = logging.getLogger("talos.security")
security_logger.setLevel(logging.INFO)
handler = logging.FileHandler(SECURITY_LOG)
handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
security_logger.addHandler(handler)


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
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    peername = request.transport.get_extra_info("peername")
    if peername:
        return peername[0]
    return "unknown"


def _is_secure(request):
    if request.scheme == "https":
        return True
    proto = request.headers.get("X-Forwarded-Proto", "")
    return proto.lower() == "https"


def get_tailscale_ip():
    if not shutil.which("tailscale"):
        return None
    result = subprocess.run(
        ["tailscale", "ip", "-4"],
        capture_output=True, text=True, timeout=5
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return None


def get_tailscale_hostname():
    if not shutil.which("tailscale"):
        return None
    result = subprocess.run(
        ["tailscale", "status", "--json"],
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
    del csrf_tokens[token]
    return True


def cleanup_csrf():
    now = time.time()
    expired = [t for t, ts in csrf_tokens.items() if now - ts > CSRF_MAX_AGE]
    for t in expired:
        del csrf_tokens[t]


def check_tailscale():
    if not shutil.which("tailscale"):
        return False, "not installed"
    result = subprocess.run(
        ["tailscale", "status", "--json"],
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
    if not shutil.which("tailscale"):
        return False, "tailscale not installed"
    result = subprocess.run(
        ["tailscale", "funnel", "status"],
        capture_output=True, text=True, timeout=5
    )
    if result.returncode == 0 and str(WEB_PORT) in result.stdout:
        return True, "active"
    result2 = subprocess.run(
        ["tailscale", "serve", "status"],
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
        html = f.read()
    for key, value in kwargs.items():
        html = html.replace("{{" + key + "}}", str(value))
    return html


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


async def handle_root(request):
    token = request.cookies.get(SESSION_COOKIE)
    if validate_session(token):
        response = web.Response(
            text=render_template("dashboard.html", BOT_NAME=BOT_NAME),
            content_type="text/html",
        )
        return response
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


async def handle_dashboard(request):
    token = request.cookies.get(SESSION_COOKIE)
    if not validate_session(token):
        return web.HTTPFound("/")
    return web.Response(text=render_template("dashboard.html", BOT_NAME=BOT_NAME), content_type="text/html")


async def handle_keys(request):
    token = request.cookies.get(SESSION_COOKIE)
    if not validate_session(token):
        return web.HTTPFound("/")
    return web.Response(text=render_template("keys.html", BOT_NAME=BOT_NAME), content_type="text/html")


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


async def handle_api_keys_get(request):
    return web.json_response(_read_env_keys())


async def handle_api_keys_post(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid request."}, status=400)

    env_vars = _read_env_file()

    for mk in MANAGED_KEYS:
        ek = mk["env_key"]
        if ek in body and body[ek].strip():
            env_vars[ek] = body[ek].strip()

    with open(ENV_FILE, "w") as f:
        for k, v in env_vars.items():
            f.write(f"{k}={v}\n")

    AI.reload_clients()
    import websearch
    import firecrawl
    websearch.reload_client()
    firecrawl.reload_client()

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
        return web.json_response({"error": "No credentials configured on server."}, status=500)

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

    return web.json_response({
        "bot": {"ok": True, "detail": "online"},
        "tailscale": {"ok": ts_ok, "detail": ts_detail},
        "funnel": {"ok": fn_ok, "detail": fn_detail},
        "venv": {"ok": venv_ok, "detail": venv_detail},
        "credentials": {"ok": creds_ok, "detail": creds_detail},
        "uptime": uptime,
    })


@require_auth
async def handle_settings(request):
    return web.Response(text=render_template("settings.html", BOT_NAME=BOT_NAME), content_type="text/html")


@require_auth
async def handle_tools(request):
    return web.Response(text=render_template("tools.html", BOT_NAME=BOT_NAME), content_type="text/html")


@require_auth
async def handle_projects_page(request):
    return web.Response(text=render_template("projects.html", BOT_NAME=BOT_NAME), content_type="text/html")


@require_auth
async def handle_api_settings_get(request):
    env_vars = _read_env_file()
    return web.json_response({
        "BOT_NAME": env_vars.get("BOT_NAME", ""),
        "TELEGRAM_BOT_TOKEN": env_vars.get("TELEGRAM_BOT_TOKEN", ""),
        "WEB_PORT": env_vars.get("WEB_PORT", "8080"),
        "ZHIPUAI_API_KEY": env_vars.get("ZHIPUAI_API_KEY", ""),
        "GEMINI_API_KEY": env_vars.get("GEMINI_API_KEY", ""),
        "OPENAI_API_KEY": env_vars.get("OPENAI_API_KEY", ""),
        "ANTHROPIC_API_KEY": env_vars.get("ANTHROPIC_API_KEY", ""),
        "FIRECRAWL_API_KEY": env_vars.get("FIRECRAWL_API_KEY", ""),
        "PIPER_VOICE": env_vars.get("PIPER_VOICE", "en_US-lessac-medium"),
        "CLIENT_BASE_URL": env_vars.get("CLIENT_BASE_URL", "https://api.z.ai/api/coding/paas/v4"),
        "MAX_TOOL_ROUNDS": env_vars.get("MAX_TOOL_ROUNDS", "5"),
        "MAX_TOOL_CALLS_PER_ROUND": env_vars.get("MAX_TOOL_CALLS_PER_ROUND", "20"),
        "MAX_COMMAND_TIMEOUT": env_vars.get("MAX_COMMAND_TIMEOUT", "120"),
        "MAX_WORKFLOW_STEPS": env_vars.get("MAX_WORKFLOW_STEPS", "12"),
        "MAX_SUBAGENT_TOOL_ROUNDS": env_vars.get("MAX_SUBAGENT_TOOL_ROUNDS", "5"),
        "MAX_SUBAGENT_TOOL_CALLS_PER_ROUND": env_vars.get("MAX_SUBAGENT_TOOL_CALLS_PER_ROUND", "15"),
    })


@require_auth
async def handle_api_settings_post(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid request."}, status=400)

    env_vars = _read_env_file()

    _TEXT_KEYS = [
        "BOT_NAME", "TELEGRAM_BOT_TOKEN", "WEB_PORT",
        "ZHIPUAI_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "FIRECRAWL_API_KEY",
        "PIPER_VOICE", "CLIENT_BASE_URL",
    ]
    _INT_KEYS = [
        ("MAX_TOOL_ROUNDS", 1, 50),
        ("MAX_TOOL_CALLS_PER_ROUND", 1, 100),
        ("MAX_COMMAND_TIMEOUT", 5, 600),
        ("MAX_WORKFLOW_STEPS", 1, 50),
        ("MAX_SUBAGENT_TOOL_ROUNDS", 1, 50),
        ("MAX_SUBAGENT_TOOL_CALLS_PER_ROUND", 1, 100),
    ]

    for key in _TEXT_KEYS:
        if key in body and body[key].strip():
            env_vars[key] = body[key].strip()

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

    load_dotenv(override=True)

    return web.json_response({"ok": True})


@require_auth
async def handle_api_tools_get(request):
    config_file = os.path.join(SCRIPT_DIR, ".tools_config")
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
        "web_search": True,
        "scrape_url": True,
        "read_file": True,
        "write_file": True,
        "edit_file": True,
        "spawn_subagent": True,
        "send_telegram_message": True,
        "send_voice_message": True,
        "create_project": True,
        "list_projects": True,
    }

    for tool_id in all_tools:
        if tool_id not in enabled_tools:
            enabled_tools[tool_id] = True

    return web.json_response(enabled_tools)


@require_auth
async def handle_api_tools_post(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid request."}, status=400)

    config_file = os.path.join(SCRIPT_DIR, ".tools_config")

    with open(config_file, "w") as f:
        json.dump(body, f, indent=2)

    return web.json_response({"ok": True})


@require_auth
async def handle_api_reload(request):
    try:
        load_dotenv(override=True)
        AI.reload_clients()
        import websearch
        import firecrawl
        websearch.reload_client()
        firecrawl.reload_client()
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


@require_auth
async def handle_api_restart(request):
    try:
        if sys.platform == "win32":
            subprocess.Popen([sys.executable, os.path.join(SCRIPT_DIR, "telegram_bot.py")])
            sys.exit(0)
        else:
            os.execv(sys.executable, [sys.executable, os.path.join(SCRIPT_DIR, "telegram_bot.py")])
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def main():
    global start_time
    start_time = time.time()

    cron_stop = asyncio.Event()

    db.init()
    memory.init()
    gateway.init()
    load_credentials()

    tg_app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )
    register_handlers(tg_app)

    web_app = web.Application()
    web_app.router.add_get("/", handle_root)
    web_app.router.add_get("/dashboard", handle_dashboard)
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
    web_app.router.add_get("/api/tools", handle_api_tools_get)
    web_app.router.add_post("/api/tools", handle_api_tools_post)
    web_app.router.add_post("/api/reload", handle_api_reload)
    web_app.router.add_post("/api/restart", handle_api_restart)
    web_app.router.add_static("/static", STATIC_DIR)
    gateway.setup_routes(web_app)

    runner = web.AppRunner(web_app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()

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

    print("Bot is running. Press Ctrl+C to stop.")

    for attempt in range(1, 4):
        try:
            await tg_app.initialize()
            break
        except Exception as e:
            if attempt == 3:
                print(f"Failed to connect to Telegram after 3 attempts: {e}")
                await runner.cleanup()
                sys.exit(1)
            print(f"Telegram connection attempt {attempt} failed ({e}), retrying in 5s...")
            await asyncio.sleep(5)

    await tg_app.start()
    await tg_app.updater.start_polling()

    cron_task = asyncio.create_task(cron_jobs.cron_loop(cron_stop))

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        cron_stop.set()
        cron_task.cancel()
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
