"""Model-free system check and self-repair.

Every check here is a deterministic probe: run a binary, stat a file, open the
database, hit a provider's model-list endpoint. Nothing in this module calls an
LLM, and nothing in it depends on an agent deciding what to do — the whole point
is that it still works when the agent is the thing that is broken.

Checks return a status and, where the failure is one we recognise, the id of a
repair that fixes it. Repairs are split in two:

  - `fix`     — safe, non-destructive, no user input (reinstall a pinned binary,
                write a missing default config, create a directory).
  - `reset`   — destructive: deletes the existing configuration so it can be
                rebuilt to current spec. Always requires explicit confirmation
                and may require the user to re-enter credentials.
"""

import asyncio
import json
import os
import shutil
import sqlite3
import time
from typing import Any, Awaitable, Callable

import app_paths
import email_tools
import model_router
import tool_probes

OK = "ok"
WARN = "warn"
FAIL = "fail"
SKIP = "skip"


class CheckResult:
    def __init__(
        self,
        check_id: str,
        name: str,
        category: str,
        status: str,
        detail: str,
        fix: str | None = None,
        reset: str | None = None,
        hint: str = "",
        data: dict | None = None,
    ):
        self.check_id = check_id
        self.name = name
        self.category = category
        self.status = status
        self.detail = detail
        self.fix = fix
        self.reset = reset
        self.hint = hint
        self.data = data or {}

    def to_dict(self) -> dict:
        return {
            "id": self.check_id,
            "name": self.name,
            "category": self.category,
            "status": self.status,
            "detail": self.detail,
            "fix": self.fix,
            "reset": self.reset,
            "hint": self.hint,
            "data": self.data,
        }


# ---------------------------------------------------------------------------
# Environment file helpers (shared with the repair actions)
# ---------------------------------------------------------------------------

def _env_file() -> str:
    return app_paths.env_file_path()


def _read_env() -> dict[str, str]:
    path = _env_file()
    values: dict[str, str] = {}
    if not os.path.isfile(path):
        return values
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip()
    return values


def _write_env(values: dict[str, str]) -> None:
    path = _env_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for key, val in values.items():
            fh.write(f"{key}={val}\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

async def check_env_file() -> CheckResult:
    path = _env_file()
    if not os.path.isfile(path):
        return CheckResult(
            "env.file", "Environment file", "core", FAIL,
            f"No .env at {path}. TALOS has no configuration to read.",
            fix="env.create",
            hint="Creates an empty .env so setup steps have somewhere to write.",
        )
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        mode = 0
    if mode & 0o077:
        return CheckResult(
            "env.file", "Environment file", "core", WARN,
            f".env is readable by other users on this machine (mode {mode:o}). It holds API keys and "
            "your email app password.",
            fix="env.chmod",
            hint="Restricts .env to owner-only (chmod 600).",
        )
    return CheckResult("env.file", "Environment file", "core", OK, f"Present at {path}, owner-only.")


async def check_database() -> CheckResult:
    path = app_paths.db_path()
    if not os.path.isfile(path):
        return CheckResult(
            "db.file", "Database", "core", WARN,
            "No database file yet — it will be created on first use.",
        )
    try:
        conn = sqlite3.connect(path, timeout=5)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]
        finally:
            conn.close()
    except Exception as exc:
        return CheckResult(
            "db.file", "Database", "core", FAIL,
            f"Cannot open the database: {exc}",
            reset="db.reset",
            hint="Moves the corrupt database aside and creates a fresh one. Chat history and "
                 "scheduled jobs are lost; a timestamped backup is kept.",
        )
    if not integrity or integrity[0] != "ok":
        return CheckResult(
            "db.file", "Database", "core", FAIL,
            f"Integrity check failed: {integrity[0] if integrity else 'unknown'}",
            reset="db.reset",
            hint="Moves the corrupt database aside and creates a fresh one.",
        )
    return CheckResult(
        "db.file", "Database", "core", OK,
        f"Healthy, {len(tables)} tables.", data={"tables": len(tables)},
    )


async def check_data_dirs() -> CheckResult:
    missing = []
    required = {
        "data root": app_paths.data_root(),
        "logs": app_paths.logs_dir(),
        "bin": app_paths.bin_dir(),
    }
    for label, path in required.items():
        if not os.path.isdir(path):
            missing.append(f"{label} ({path})")
    if missing:
        return CheckResult(
            "core.dirs", "Data directories", "core", FAIL,
            "Missing: " + ", ".join(missing),
            fix="core.dirs.create",
            hint="Creates the missing directories.",
        )
    if not os.access(app_paths.data_root(), os.W_OK):
        return CheckResult(
            "core.dirs", "Data directories", "core", FAIL,
            f"Data root {app_paths.data_root()} is not writable by this process.",
            hint="Fix the filesystem permissions on that directory, then re-run the check.",
        )
    return CheckResult("core.dirs", "Data directories", "core", OK, "All present and writable.")


async def check_himalaya_binary() -> CheckResult:
    """The check that would have caught the email outage."""
    info = await email_tools.probe_himalaya_version(force=True)
    binary = os.getenv("HIMALAYA_BIN", "").strip() or "himalaya"

    if not info.get("ok"):
        return CheckResult(
            "email.binary", "Himalaya CLI", "email", FAIL,
            info.get("error", "Himalaya CLI is not installed."),
            fix="email.install_cli",
            hint=f"Downloads the pinned Himalaya v{email_tools.HIMALAYA_TARGET_VERSION} build and "
                 "points HIMALAYA_BIN at it.",
            data={"binary": binary},
        )

    if not info.get("supported"):
        return CheckResult(
            "email.binary", "Himalaya CLI", "email", FAIL,
            f"Installed version is {info.get('version_str') or 'unrecognised'}, but TALOS drives the "
            f"v{email_tools.HIMALAYA_TARGET_VERSION} command set. Every email operation will fail with "
            "an argument error until this is corrected.",
            fix="email.install_cli",
            hint=f"Replaces it with the pinned v{email_tools.HIMALAYA_TARGET_VERSION} build.",
            data={"binary": binary, "installed": info.get("version_str", "")},
        )

    return CheckResult(
        "email.binary", "Himalaya CLI", "email", OK,
        f"v{info.get('version_str')} at {binary}.",
        data={"binary": binary, "installed": info.get("version_str", "")},
    )


async def check_himalaya_config() -> CheckResult:
    raw = os.getenv("HIMALAYA_CONFIG", "").strip()
    if not raw:
        return CheckResult(
            "email.config", "Email account config", "email", WARN,
            "HIMALAYA_CONFIG is not set — email is not configured.",
            reset="email.reconfigure",
            hint="Opens email setup so you can enter a Gmail address and app password.",
        )

    resolved = email_tools.resolve_config_path()
    if not resolved:
        return CheckResult(
            "email.config", "Email account config", "email", FAIL,
            f"HIMALAYA_CONFIG is '{raw}', but no config file exists there. Himalaya will fall back to "
            "its interactive setup wizard and every email call will fail.",
            reset="email.reconfigure",
            hint="Clears the stale setting and re-runs email setup.",
        )

    try:
        mode = os.stat(resolved).st_mode & 0o777
    except OSError:
        mode = 0
    if mode & 0o077:
        return CheckResult(
            "email.config", "Email account config", "email", WARN,
            f"Config at {resolved} is readable by other users (mode {mode:o}) and contains your "
            "email app password.",
            fix="email.chmod_config",
            hint="Restricts the config to owner-only (chmod 600).",
            data={"path": resolved},
        )

    return CheckResult(
        "email.config", "Email account config", "email", OK,
        f"Present at {resolved}.", data={"path": resolved},
    )


async def check_email_live() -> CheckResult:
    """End-to-end, through the same code path the agent uses."""
    if not email_tools.resolve_config_path():
        return CheckResult(
            "email.live", "Email connection", "email", SKIP,
            "Skipped — email is not configured yet.",
        )

    try:
        result = await asyncio.wait_for(email_tools.execute("list_folders"), timeout=60)
    except asyncio.TimeoutError:
        return CheckResult(
            "email.live", "Email connection", "email", FAIL,
            "Timed out after 60s connecting to the mail server.",
            hint="Usually a network block on IMAP port 993, or a firewall rule.",
        )
    except Exception as exc:
        return CheckResult(
            "email.live", "Email connection", "email", FAIL, f"Unexpected error: {exc}",
        )

    if result.get("ok"):
        return CheckResult(
            "email.live", "Email connection", "email", OK,
            "Connected and listed folders successfully.",
        )

    detail = str(result.get("error") or "Unknown failure")
    if result.get("version_mismatch"):
        return CheckResult(
            "email.live", "Email connection", "email", FAIL, detail,
            fix="email.install_cli",
            hint=f"Installs the pinned v{email_tools.HIMALAYA_TARGET_VERSION} build.",
        )
    lowered = detail.lower()
    if "auth" in lowered or "credential" in lowered or "login" in lowered:
        return CheckResult(
            "email.live", "Email connection", "email", FAIL,
            f"Authentication rejected: {detail}",
            reset="email.reconfigure",
            hint="Gmail app passwords are revoked when the account password changes. "
                 "Re-running setup lets you enter a new one.",
        )
    return CheckResult(
        "email.live", "Email connection", "email", FAIL, detail,
        reset="email.reconfigure",
        hint="Deletes the current email config and re-runs setup to current spec.",
    )


async def check_terminal() -> CheckResult:
    import terminal_tools

    try:
        executor = terminal_tools.get_executor()
    except Exception as exc:
        return CheckResult(
            "terminal.executor", "Command execution", "tools", FAIL,
            f"Executor failed to start: {exc}",
            fix="terminal.reset_config",
            hint="Rewrites the terminal config to safe native-sandbox defaults.",
        )

    mode = executor.sandbox_mode
    if mode == "docker" and executor.docker_client is None:
        return CheckResult(
            "terminal.executor", "Command execution", "tools", FAIL,
            "Configured for Docker sandboxing, but the Docker daemon is unreachable.",
            fix="terminal.reset_config",
            hint="Switches the sandbox mode to native so commands can run.",
        )

    try:
        probe = await asyncio.wait_for(executor.execute("echo talos_ok", timeout=10), timeout=20)
    except Exception as exc:
        return CheckResult(
            "terminal.executor", "Command execution", "tools", FAIL,
            f"Probe command failed: {exc}",
        )

    if probe.get("rate_limited"):
        return CheckResult(
            "terminal.executor", "Command execution", "tools", WARN,
            f"Rate limiter is currently saturated (limit {executor.max_commands_per_minute}/min); "
            f"retry in ~{probe.get('retry_after_s')}s. This is expected right after heavy tool use.",
            data={"limit": executor.max_commands_per_minute},
        )
    if "talos_ok" not in str(probe.get("stdout", "")):
        return CheckResult(
            "terminal.executor", "Command execution", "tools", FAIL,
            f"Probe command did not return the expected output: {json.dumps(probe)[:200]}",
        )

    limit = executor.max_commands_per_minute
    if limit < 20:
        return CheckResult(
            "terminal.executor", "Command execution", "tools", WARN,
            f"Working ({mode} mode), but the limit of {limit} commands/minute is low enough that a "
            "multi-step task will stall partway through.",
            fix="terminal.raise_rate_limit",
            hint="Raises the limit to 30 commands per minute.",
            data={"mode": mode, "limit": limit},
        )

    return CheckResult(
        "terminal.executor", "Command execution", "tools", OK,
        f"Working in {mode} mode, {limit} commands/minute.",
        data={"mode": mode, "limit": limit},
    )


async def check_model_providers() -> CheckResult:
    """Key presence and reachability only — this never runs an inference."""
    enabled = []
    for provider in ("openai", "anthropic", "gemini", "nvidia", "cerebras", "openrouter", "zhipu", "ollama"):
        try:
            if model_router._provider_enabled(provider):
                enabled.append(provider)
        except Exception:
            continue

    if not enabled:
        return CheckResult(
            "model.providers", "Model providers", "model", FAIL,
            "No model provider is configured. Set at least one API key in Settings, or configure Ollama.",
            hint="Without a provider the assistant cannot respond at all.",
        )

    return CheckResult(
        "model.providers", "Model providers", "model", OK,
        f"{len(enabled)} configured: {', '.join(enabled)}.",
        data={"providers": enabled},
    )


async def check_ollama() -> CheckResult:
    if not os.getenv("OLLAMA_MODEL", "").strip():
        return CheckResult("model.ollama", "Ollama", "model", SKIP, "Not configured.")

    # OLLAMA_BASE_URL points at the OpenAI-compatible shim (".../v1"); the
    # native /api/tags endpoint lives one level up.
    base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    try:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{base}/api/tags") as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                payload = await resp.json()
    except Exception as exc:
        return CheckResult(
            "model.ollama", "Ollama", "model", FAIL,
            f"Ollama is set as a provider but {base} is unreachable: {exc}",
            hint="Start the Ollama service, or clear OLLAMA_MODEL if you no longer use it.",
        )

    names = [m.get("name", "") for m in payload.get("models", [])]
    wanted = os.getenv("OLLAMA_MODEL", "").strip()
    if wanted and wanted not in names:
        return CheckResult(
            "model.ollama", "Ollama", "model", WARN,
            f"Reachable, but the configured model '{wanted}' is not pulled. Available: "
            f"{', '.join(names[:5]) or 'none'}.",
            hint=f"Run: ollama pull {wanted}",
            data={"models": names},
        )
    return CheckResult(
        "model.ollama", "Ollama", "model", OK,
        f"Reachable at {base}, {len(names)} model(s) available.", data={"models": names},
    )


async def check_tailscale_https() -> CheckResult:
    """The dashboard serves plain HTTP; Tailscale is what puts HTTPS in front."""
    import telegram_bot

    if not telegram_bot._resolve_tailscale_bin():
        return CheckResult(
            "network.https", "HTTPS (Tailscale)", "network", WARN,
            "Tailscale is not installed, so the dashboard is reachable over plain HTTP only.",
            hint="Install Tailscale from https://tailscale.com/download, sign in, then re-run this check.",
        )

    connected, detail = await asyncio.to_thread(telegram_bot.check_tailscale)
    if not connected:
        return CheckResult(
            "network.https", "HTTPS (Tailscale)", "network", WARN,
            f"Tailscale is installed but not connected ({detail}), so HTTPS cannot be set up.",
            hint="Run `tailscale up` and sign in, then re-run this check.",
        )

    # No permission pre-check here on purpose. Tailscale only refuses writes, so
    # nothing readable reveals whether this account may configure it — the
    # repair attempts the real operation and grants operator if it is denied.
    mode = telegram_bot.TAILSCALE_HTTPS_MODE
    if mode == "off":
        return CheckResult(
            "network.https", "HTTPS (Tailscale)", "network", SKIP,
            "Disabled by configuration (TAILSCALE_HTTPS_MODE=off).",
        )

    state = await asyncio.to_thread(telegram_bot.tailscale_https_state)

    if state["ok"]:
        return CheckResult(
            "network.https", "HTTPS (Tailscale)", "network", OK,
            state["detail"] + (f" Reachable at {state['url']}." if state["url"] else ""),
            data={"mode": state["mode"], "url": state["url"]},
        )

    if not state["configured"]:
        # Nothing is set up. This is the case one click actually fixes.
        return CheckResult(
            "network.https", "HTTPS (Tailscale)", "network", FAIL,
            f"The dashboard is served over plain HTTP. {state['detail']}",
            fix="network.enable_https",
            hint=f"Runs `tailscale {mode}` so Tailscale terminates HTTPS in front of port "
                 f"{telegram_bot.WEB_PORT}.",
            data={"mode": mode},
        )

    # Configured but not answering. Re-running `serve` will not help; the useful
    # repair is to wipe the config and lay it down again, and if two daemons are
    # running that is a machine problem no repair here can fix.
    if state["daemons"] > 1:
        return CheckResult(
            "network.https", "HTTPS (Tailscale)", "network", FAIL,
            state["detail"],
            reset="network.reset_https",
            hint="Remove the duplicate Tailscale installation, then reset and re-enable HTTPS.",
            data={"daemons": state["daemons"], "mode": state["mode"]},
        )

    return CheckResult(
        "network.https", "HTTPS (Tailscale)", "network", WARN,
        state["detail"],
        reset="network.reset_https",
        hint="Clears the Tailscale serve config and applies it again from scratch.",
        data={"mode": state["mode"], "url": state["url"]},
    )


_SERVICE_NAME = "clai-talos"


def _clai_config() -> dict:
    """Whatever start.sh recorded about the background install."""
    path = os.path.join(os.path.expanduser("~"), ".config", "clai-talos", "env")
    values: dict[str, str] = {}
    if not os.path.isfile(path):
        return values
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip().strip('"')
    except OSError:
        pass
    return values


async def check_background_service() -> CheckResult:
    """Is TALOS installed as a service that survives logout and reboot?"""
    config = _clai_config()
    manager = config.get("SERVICE_MANAGER", "")

    if not config:
        return CheckResult(
            "service.background", "Background service", "service", WARN,
            "TALOS is not installed as a background service, so it stops when this terminal or "
            "SSH session closes.",
            fix="service.install_background",
            hint="Installs a service so it survives logout and restarts at boot, and adds the "
                 "'clai' command.",
        )

    if manager == "systemd":
        try:
            active = await asyncio.create_subprocess_exec(
                "systemctl", "is-active", "--quiet", _SERVICE_NAME,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(active.wait(), timeout=15)
            running = active.returncode == 0

            enabled_proc = await asyncio.create_subprocess_exec(
                "systemctl", "is-enabled", "--quiet", _SERVICE_NAME,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(enabled_proc.wait(), timeout=15)
            at_boot = enabled_proc.returncode == 0
        except Exception as exc:
            return CheckResult(
                "service.background", "Background service", "service", WARN,
                f"Could not query systemd for {_SERVICE_NAME}: {exc}",
            )

        if running and at_boot:
            return CheckResult(
                "service.background", "Background service", "service", OK,
                f"Running under systemd as {_SERVICE_NAME}, and enabled at boot.",
            )
        if running:
            return CheckResult(
                "service.background", "Background service", "service", WARN,
                f"{_SERVICE_NAME} is running but not enabled at boot — it will not come back "
                "after a restart.",
                fix="service.enable_boot",
                hint=f"Runs `systemctl enable {_SERVICE_NAME}`.",
            )
        # Not running, yet something is running this code — so this is almost
        # certainly a foreground run alongside a stopped service.
        return CheckResult(
            "service.background", "Background service", "service", WARN,
            f"The {_SERVICE_NAME} service is installed but not active. TALOS appears to be "
            "running in the foreground instead.",
            hint="Start the service with `clai start`, or keep running in the foreground.",
        )

    if manager == "launchd":
        return CheckResult(
            "service.background", "Background service", "service", OK,
            f"Managed by launchd as {_SERVICE_NAME} (starts at login).",
        )

    if manager == "process":
        return CheckResult(
            "service.background", "Background service", "service", WARN,
            "Running as a plain background process — it survives logout but not a reboot.",
            fix="service.install_background",
            hint="Re-runs the service installer, which will use systemd or launchd if either is "
                 "available on this machine.",
        )

    return CheckResult(
        "service.background", "Background service", "service", WARN,
        f"Unrecognised service manager '{manager}' in the clai config.",
    )


async def check_google() -> CheckResult:
    creds = app_paths.oauth_tokens_path()
    has_key = bool(os.getenv("GOOGLE_API_KEY", "").strip())
    has_oauth = os.path.isfile(creds)

    if not has_key and not has_oauth:
        return CheckResult("google.integration", "Google Workspace", "integrations", SKIP, "Not configured.")
    if has_oauth:
        try:
            with open(creds, "r", encoding="utf-8") as fh:
                json.load(fh)
        except Exception as exc:
            return CheckResult(
                "google.integration", "Google Workspace", "integrations", FAIL,
                f"OAuth token file at {creds} is unreadable or corrupt: {exc}",
                reset="google.reset",
                hint="Deletes the stored token so you can reconnect from Settings.",
            )
    return CheckResult(
        "google.integration", "Google Workspace", "integrations", OK,
        ("API key set. " if has_key else "") + ("OAuth connected." if has_oauth else ""),
    )


async def check_telegram() -> CheckResult:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return CheckResult(
            "telegram.token", "Telegram bot", "integrations", SKIP,
            "No bot token set — the Telegram interface is disabled.",
        )
    try:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"https://api.telegram.org/bot{token}/getMe") as resp:
                payload = await resp.json()
    except Exception as exc:
        return CheckResult(
            "telegram.token", "Telegram bot", "integrations", WARN,
            f"Could not reach Telegram to validate the token: {exc}",
        )
    if not payload.get("ok"):
        return CheckResult(
            "telegram.token", "Telegram bot", "integrations", FAIL,
            f"Telegram rejected the bot token: {payload.get('description', 'unknown error')}",
            hint="Re-issue the token with @BotFather and update it in Settings.",
        )
    return CheckResult(
        "telegram.token", "Telegram bot", "integrations", OK,
        f"Authenticated as @{payload.get('result', {}).get('username', '?')}.",
    )


async def check_tool_registry() -> CheckResult:
    """Every tool the model can call must actually dispatch to something."""
    try:
        import AI
        definitions = AI._get_all_tools(include_subagent=True, include_telegram=True)
    except Exception as exc:
        return CheckResult(
            "tools.registry", "Tool registry", "tools", FAIL,
            f"Tool definitions failed to build: {exc}",
        )

    names = [d.get("function", {}).get("name", "") for d in definitions]
    duplicates = sorted({n for n in names if names.count(n) > 1 and n})
    if duplicates:
        return CheckResult(
            "tools.registry", "Tool registry", "tools", FAIL,
            f"Duplicate tool names would shadow each other: {', '.join(duplicates)}.",
        )

    unroutable = []
    for category, (_, tool_names) in AI._TOOL_CATEGORIES.items():
        for tool_name in tool_names:
            if tool_name not in names:
                unroutable.append(f"{tool_name} (category {category})")
    if unroutable:
        return CheckResult(
            "tools.registry", "Tool registry", "tools", FAIL,
            "Lazy-load categories reference tools that no longer exist: " + ", ".join(unroutable) + ". "
            "Asking for those categories would load nothing.",
        )

    return CheckResult(
        "tools.registry", "Tool registry", "tools", OK,
        f"{len(names)} tools registered, all categories resolve.",
        data={"count": len(names)},
    )


async def check_disk() -> CheckResult:
    try:
        usage = shutil.disk_usage(app_paths.data_root())
    except Exception as exc:
        return CheckResult("core.disk", "Disk space", "core", WARN, f"Could not read disk usage: {exc}")
    free_gb = usage.free / (1024 ** 3)
    if free_gb < 1:
        return CheckResult(
            "core.disk", "Disk space", "core", FAIL,
            f"Only {free_gb:.1f} GB free. Logs, the database and downloads will start failing.",
        )
    if free_gb < 5:
        return CheckResult(
            "core.disk", "Disk space", "core", WARN, f"{free_gb:.1f} GB free.",
        )
    return CheckResult("core.disk", "Disk space", "core", OK, f"{free_gb:.1f} GB free.")


async def check_tool_coverage() -> CheckResult:
    """Every registered tool must be either probed or explicitly excused.

    This is what stops the suite silently rotting: add a tool without a probe
    and this check tells you, instead of the tool going untested forever.
    """
    try:
        import AI
        registered = {
            d.get("function", {}).get("name", "")
            for d in AI._get_all_tools(include_subagent=True, include_telegram=True)
        }
    except Exception as exc:
        return CheckResult("tools.coverage", "Test coverage", "tools", FAIL,
                           f"Could not read the tool registry: {exc}")

    registered.discard("")
    accounted = tool_probes.covered_tools() | set(tool_probes.UNPROBED_TOOLS)
    untested = sorted(registered - accounted)
    stale = sorted(accounted - registered - {"load_tools"})

    if untested:
        return CheckResult(
            "tools.coverage", "Test coverage", "tools", WARN,
            f"{len(untested)} tool(s) have no probe and are not listed as excused: "
            + ", ".join(untested) + ".",
            hint="Add a probe in tool_probes.py, or list the tool in UNPROBED_TOOLS with a reason.",
            data={"untested": untested},
        )
    if stale:
        return CheckResult(
            "tools.coverage", "Test coverage", "tools", WARN,
            "Probes reference tools that no longer exist: " + ", ".join(stale) + ".",
            data={"stale": stale},
        )

    excused = len(tool_probes.UNPROBED_TOOLS)
    return CheckResult(
        "tools.coverage", "Test coverage", "tools", OK,
        f"All {len(registered)} tools accounted for: {len(registered) - excused} probed, "
        f"{excused} excused with a documented reason.",
        data={"registered": len(registered), "excused": excused},
    )


def _make_tool_check(probe_id: str, name: str, category: str, fn) -> Callable[[], Awaitable[CheckResult]]:
    async def check() -> CheckResult:
        try:
            outcome = await fn()
        except Exception as exc:
            return CheckResult(probe_id, name, category, FAIL,
                               f"The probe itself raised an error: {type(exc).__name__}: {exc}")
        return CheckResult(
            probe_id, name, category, outcome.status, outcome.detail,
            fix=outcome.fix, reset=outcome.reset, hint=outcome.hint,
        )
    check.__name__ = f"check_{probe_id.replace('.', '_')}"
    return check


TOOL_CHECKS: list[Callable[[], Awaitable[CheckResult]]] = [
    _make_tool_check(pid, name, cat, fn) for pid, name, cat, fn in tool_probes.PROBES
]


CHECKS: list[Callable[[], Awaitable[CheckResult]]] = [
    check_env_file,
    check_data_dirs,
    check_disk,
    check_database,
    check_tool_registry,
    check_terminal,
    check_model_providers,
    check_ollama,
    check_himalaya_binary,
    check_himalaya_config,
    check_email_live,
    check_tailscale_https,
    check_background_service,
    check_google,
    check_telegram,
    check_tool_coverage,
] + TOOL_CHECKS


async def run_all() -> dict:
    started = time.time()
    results: list[dict] = []

    for check in CHECKS:
        try:
            result = await check()
        except Exception as exc:
            # A check that crashes is itself a finding — never let one broken
            # probe take down the whole report.
            name = getattr(check, "__name__", "unknown")
            result = CheckResult(name, name, "core", FAIL, f"Check itself raised an error: {exc}")
        results.append(result.to_dict())

    counts = {OK: 0, WARN: 0, FAIL: 0, SKIP: 0}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    if counts[FAIL]:
        overall = FAIL
    elif counts[WARN]:
        overall = WARN
    else:
        overall = OK

    return {
        "ok": True,
        "overall": overall,
        "counts": counts,
        "checks": results,
        "duration_s": round(time.time() - started, 2),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# ---------------------------------------------------------------------------
# Repairs
# ---------------------------------------------------------------------------

async def _repair_env_create() -> dict:
    path = _env_file()
    if os.path.isfile(path):
        return {"ok": True, "message": ".env already exists — nothing to do."}
    _write_env({})
    return {"ok": True, "message": f"Created {path}."}


async def _repair_env_chmod() -> dict:
    path = _env_file()
    if not os.path.isfile(path):
        return {"ok": False, "message": "No .env to secure."}
    os.chmod(path, 0o600)
    return {"ok": True, "message": "Restricted .env to owner-only."}


async def _repair_dirs_create() -> dict:
    created = []
    for path in (app_paths.data_root(), app_paths.logs_dir(), app_paths.bin_dir()):
        if not os.path.isdir(path):
            os.makedirs(path, exist_ok=True)
            created.append(path)
    return {
        "ok": True,
        "message": ("Created: " + ", ".join(created)) if created else "All directories already present.",
    }


async def _repair_install_himalaya() -> dict:
    """Install the pinned CLI and point HIMALAYA_BIN at it."""
    import telegram_bot

    bin_path = await telegram_bot._download_himalaya(force=True)
    if not bin_path:
        return {
            "ok": False,
            "message": (
                f"Could not install Himalaya v{email_tools.HIMALAYA_TARGET_VERSION}. Check network access "
                f"to github.com, or install it manually and set HIMALAYA_BIN in Settings."
            ),
        }

    env = _read_env()
    env["HIMALAYA_BIN"] = bin_path
    _write_env(env)
    os.environ["HIMALAYA_BIN"] = bin_path
    email_tools.reset_version_cache()

    info = await email_tools.probe_himalaya_version(force=True)
    if not info.get("supported"):
        return {"ok": False, "message": f"Installed, but version check still fails: {info.get('error')}"}

    return {
        "ok": True,
        "message": f"Installed Himalaya v{info.get('version_str')} at {bin_path} and updated HIMALAYA_BIN.",
    }


async def _repair_chmod_email_config() -> dict:
    resolved = email_tools.resolve_config_path()
    if not resolved:
        return {"ok": False, "message": "No email config found to secure."}
    os.chmod(resolved, 0o600)
    return {"ok": True, "message": f"Restricted {resolved} to owner-only."}


async def _repair_email_reconfigure() -> dict:
    """Destructive: delete the email config so setup can rebuild it to spec."""
    resolved = email_tools.resolve_config_path()
    removed = []

    if resolved and os.path.isfile(resolved):
        backup = f"{resolved}.bak.{int(time.time())}"
        shutil.move(resolved, backup)
        removed.append(f"{resolved} (backed up to {backup})")

    env = _read_env()
    for key in ("HIMALAYA_CONFIG", "HIMALAYA_DEFAULT_ACCOUNT"):
        if key in env:
            env.pop(key)
            os.environ.pop(key, None)
    _write_env(env)
    email_tools.reset_version_cache()

    return {
        "ok": True,
        "needs_setup": "email",
        "message": (
            "Email configuration cleared"
            + (f" ({'; '.join(removed)})" if removed else "")
            + ". Re-run email setup to enter your Gmail address and app password."
        ),
    }


async def _repair_terminal_reset_config() -> dict:
    import terminal_tools

    path = app_paths.terminal_config_path()
    config = {
        "sandbox_mode": "native",
        "require_confirmation": True,
        "max_commands_per_minute": 30,
        "default_timeout": 30,
        "dangerous_commands": sorted(terminal_tools.DANGEROUS_COMMANDS),
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
    terminal_tools._executor = None
    return {"ok": True, "message": "Terminal config reset to native sandbox, 30 commands/minute."}


async def _repair_raise_rate_limit() -> dict:
    import terminal_tools

    path = app_paths.terminal_config_path()
    config = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                config = json.load(fh)
        except Exception:
            config = {}
    config.setdefault("sandbox_mode", "native")
    config.setdefault("require_confirmation", True)
    config.setdefault("default_timeout", 30)
    config["max_commands_per_minute"] = 30
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
    terminal_tools._executor = None
    return {"ok": True, "message": "Raised the command rate limit to 30 per minute."}


async def _repair_db_reset() -> dict:
    import db

    path = app_paths.db_path()
    moved = ""
    if os.path.isfile(path):
        backup = f"{path}.corrupt.{int(time.time())}"
        shutil.move(path, backup)
        moved = f" Previous file kept at {backup}."
    db.init()
    return {"ok": True, "message": f"Created a fresh database.{moved}"}


async def _repair_google_reset() -> dict:
    path = app_paths.oauth_tokens_path()
    if os.path.isfile(path):
        backup = f"{path}.bak.{int(time.time())}"
        shutil.move(path, backup)
        return {
            "ok": True,
            "needs_setup": "google",
            "message": f"Cleared stored Google token (backup at {backup}). Reconnect from Settings.",
        }
    return {"ok": True, "message": "No stored Google token to clear."}


async def _pip_install(packages: list[str], label: str) -> dict:
    """Install packages into the running interpreter's environment."""
    import sys

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", "--upgrade", *packages,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=420)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"ok": False, "message": f"Installing {label} timed out after 7 minutes."}

    output = (out_b or b"").decode(errors="replace")
    if proc.returncode != 0:
        tail = "\n".join(output.strip().splitlines()[-6:])
        return {"ok": False, "message": f"pip install failed for {label}:\n{tail}"}

    return {
        "ok": True,
        "needs_restart": True,
        "message": f"Installed {label} ({', '.join(packages)}). Restart TALOS for it to load.",
    }


async def _repair_install_spreadsheet_deps() -> dict:
    return await _pip_install(["openpyxl", "pandas"], "spreadsheet support")


async def _repair_install_tts_deps() -> dict:
    return await _pip_install(["gTTS"], "text-to-speech")


async def _repair_install_browser_deps() -> dict:
    result = await _pip_install(["playwright"], "browser automation")
    if not result.get("ok"):
        return result

    # The package alone is useless without a browser binary.
    import sys
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "playwright", "install", "chromium",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"ok": False, "message": "Playwright installed, but downloading Chromium timed out."}

    if proc.returncode != 0:
        tail = "\n".join((out_b or b"").decode(errors="replace").strip().splitlines()[-6:])
        return {"ok": False, "message": f"Playwright installed, but Chromium download failed:\n{tail}"}

    return {
        "ok": True,
        "needs_restart": True,
        "message": "Installed Playwright and the Chromium runtime. Restart TALOS for it to load.",
    }


async def _repair_install_background_service() -> dict:
    """Install the service by delegating to start.sh --install-service.

    The unit file lives in start.sh and nowhere else; duplicating it here would
    guarantee the two drift apart.
    """
    script = os.path.join(os.path.dirname(app_paths.source_root()), "start.sh")
    if not os.path.isfile(script):
        return {"ok": False, "message": f"start.sh not found at {script}."}

    code, out = await _run_logged(["bash", script, "--install-service"], timeout=300)
    if code != 0:
        tail = "\n".join(out.splitlines()[-6:]) if out else "no output"
        return {
            "ok": False,
            "message": f"Service installation failed:\n{tail}\n"
                       "If it needs a password, run it from a terminal: ./start.sh --install-service",
        }
    return {
        "ok": True,
        "message": "Background service installed. TALOS now survives logout and restarts at boot. "
                   "Use `clai status` from a terminal.",
    }


async def _repair_clear_gemini_key() -> dict:
    """Destructive: remove a Gemini key the API rejected.

    There is no way to repair an invalid credential automatically. Clearing it
    at least returns web search to a clean "not configured" state instead of
    failing on every call, and makes it obvious a new key is needed.
    """
    env = _read_env()
    if not env.get("GEMINI_API_KEY", "").strip():
        return {"ok": True, "message": "No Gemini API key was set."}

    env["GEMINI_API_KEY"] = ""
    _write_env(env)
    os.environ["GEMINI_API_KEY"] = ""
    try:
        import websearch
        websearch.reload_client()
    except Exception:
        pass

    return {
        "ok": True,
        "needs_setup": "gemini",
        "message": "Cleared the rejected Gemini API key. Add a working one in Settings to "
                   "re-enable web search (https://aistudio.google.com/app/apikey).",
    }


async def _repair_enable_service_at_boot() -> dict:
    proc = await asyncio.create_subprocess_exec(
        "sudo", "-n", "systemctl", "enable", _SERVICE_NAME,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"ok": False, "message": "Enabling the service timed out."}

    if proc.returncode != 0:
        detail = (out_b or b"").decode(errors="replace").strip()[:200]
        return {
            "ok": False,
            "message": f"Could not enable the service automatically ({detail or 'permission denied'}). "
                       f"Run: sudo systemctl enable {_SERVICE_NAME}",
        }
    return {"ok": True, "message": f"{_SERVICE_NAME} will now start automatically at boot."}


async def _repair_enable_tailscale_https() -> dict:
    """Set up HTTPS. Registers this account as Tailscale operator if needed —
    `enable_tailscale_https` handles the permission denial internally, so there
    is no separate "grant operator" repair to get out of step with it."""
    import telegram_bot

    ok, message = await asyncio.to_thread(telegram_bot.enable_tailscale_https)
    return {"ok": ok, "message": message}


async def _repair_reset_tailscale_https() -> dict:
    """Destructive: clears the node's serve config, then sets HTTPS up fresh.

    Worth having separately because a half-written serve config (wrong port
    after a WEB_PORT change, a stale funnel entry) cannot be fixed by running
    `tailscale serve` again — it merges rather than replaces.
    """
    import telegram_bot

    cleared, clear_msg = await asyncio.to_thread(telegram_bot.disable_tailscale_https)
    if not cleared:
        return {"ok": False, "message": clear_msg}

    ok, message = await asyncio.to_thread(telegram_bot.enable_tailscale_https)
    return {"ok": ok, "message": f"{clear_msg} {message}"}


async def _run_logged(cmd: list[str], timeout: int, cwd: str | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"`{' '.join(cmd[:3])}` timed out after {timeout}s"
    return proc.returncode, (out_b or b"").decode(errors="replace").strip()


async def _install_nodejs() -> tuple[bool, str]:
    """Install Node.js with whatever package manager this machine has."""
    if shutil.which("node") and shutil.which("npm"):
        return True, "Node.js already present."

    if shutil.which("apt-get"):
        code, out = await _run_logged(
            ["sudo", "-n", "apt-get", "install", "-y", "nodejs", "npm"], timeout=600
        )
        if code == 0:
            return True, "Installed Node.js via apt."
        # A stale package index is the usual reason a fresh server fails here.
        await _run_logged(["sudo", "-n", "apt-get", "update"], timeout=300)
        code, out = await _run_logged(
            ["sudo", "-n", "apt-get", "install", "-y", "nodejs", "npm"], timeout=600
        )
        if code == 0:
            return True, "Installed Node.js via apt."
        return False, f"apt could not install Node.js: {out.splitlines()[-1][:180] if out else 'no output'}"

    if shutil.which("dnf"):
        code, out = await _run_logged(["sudo", "-n", "dnf", "install", "-y", "nodejs"], timeout=600)
        return (code == 0), ("Installed Node.js via dnf." if code == 0 else f"dnf failed: {out[-180:]}")

    if shutil.which("brew"):
        code, out = await _run_logged(["brew", "install", "node"], timeout=900)
        return (code == 0), ("Installed Node.js via Homebrew." if code == 0 else f"brew failed: {out[-180:]}")

    return False, "No supported package manager found. Install Node.js from https://nodejs.org."


async def _repair_install_docx_node() -> dict:
    """Word document creation shells out to the `docx` Node package."""
    notes: list[str] = []

    if not shutil.which("node") or not shutil.which("npm"):
        installed, message = await _install_nodejs()
        notes.append(message)
        if not installed:
            return {"ok": False, "message": message}

    npm = shutil.which("npm")
    if not npm:
        return {
            "ok": False,
            "message": " ".join(notes) + " npm is still not on PATH — a restart may be needed.",
        }

    root = app_paths.source_root()
    proc = await asyncio.create_subprocess_exec(
        npm, "install", "docx",
        cwd=root,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=420)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"ok": False, "message": "`npm install docx` timed out after 7 minutes."}

    if proc.returncode != 0:
        tail = "\n".join((out_b or b"").decode(errors="replace").strip().splitlines()[-6:])
        return {"ok": False, "message": f"`npm install docx` failed:\n{tail}"}

    notes.append(f"Installed the `docx` Node module in {root}.")
    return {"ok": True, "message": " ".join(notes)}


async def _repair_clear_scratch() -> dict:
    removed = tool_probes.clear_scratch()
    return {"ok": True, "message": f"Cleared the diagnostics scratch directory ({removed} item(s))."}


async def _repair_reset_dynamic_registry() -> dict:
    """Destructive: drops every custom tool the assistant has built."""
    import dynamic_tools

    path = app_paths.dynamic_registry_path()
    moved = ""
    if os.path.isfile(path):
        backup = f"{path}.bak.{int(time.time())}"
        shutil.copy2(path, backup)
        os.remove(path)
        moved = f" Previous registry backed up to {backup}."
    try:
        dynamic_tools._load_registry()
    except Exception:
        pass
    return {"ok": True, "message": f"Custom tool registry reset.{moved}"}


async def _repair_reset_projects_registry() -> dict:
    """Destructive: unregisters every project. Project files are left on disk."""
    import gateway

    path = app_paths.gateway_config_path()
    moved = ""
    if os.path.isfile(path):
        backup = f"{path}.bak.{int(time.time())}"
        shutil.copy2(path, backup)
        os.remove(path)
        moved = f" Previous registry backed up to {backup}."
    try:
        gateway.list_projects()
    except Exception:
        pass
    return {
        "ok": True,
        "message": f"Project registry reset — project files were left on disk.{moved}",
    }


async def _repair_full_reset() -> dict:
    """The deep option: return every repairable subsystem to a clean state.

    Deliberately does not touch the database or API keys — losing chat history
    and credentials is a bigger hammer than "my tools are broken" ever warrants,
    and those have their own targeted repairs.
    """
    steps: list[str] = []
    problems: list[str] = []

    for repair_id in (
        "core.dirs.create",
        "env.chmod",
        "terminal.reset_config",
        "files.reset_scratch",
        "email.install_cli",
        "email.reconfigure",
        "tools.reset_dynamic_registry",
        "projects.reset_registry",
        "network.reset_https",
    ):
        handler, _, label = REPAIRS[repair_id]
        try:
            outcome = await handler()
        except Exception as exc:
            problems.append(f"{label}: {exc}")
            continue
        if outcome.get("ok"):
            steps.append(label)
        else:
            problems.append(f"{label}: {outcome.get('message', 'failed')}")

    message = "Reset complete: " + ", ".join(steps) + "."
    if problems:
        message += " Could not complete: " + "; ".join(problems) + "."
    message += " Email must be reconfigured — re-enter your address and app password in setup."

    return {"ok": True, "needs_setup": "email", "message": message}


# id -> (handler, destructive, human label)
REPAIRS: dict[str, tuple[Callable[[], Awaitable[dict]], bool, str]] = {
    "env.create": (_repair_env_create, False, "Create .env"),
    "env.chmod": (_repair_env_chmod, False, "Secure .env permissions"),
    "core.dirs.create": (_repair_dirs_create, False, "Create missing directories"),
    "email.install_cli": (_repair_install_himalaya, False, "Install pinned Himalaya CLI"),
    "email.chmod_config": (_repair_chmod_email_config, False, "Secure email config permissions"),
    "email.reconfigure": (_repair_email_reconfigure, True, "Delete email config and reconfigure"),
    "terminal.reset_config": (_repair_terminal_reset_config, True, "Reset terminal config to defaults"),
    "terminal.raise_rate_limit": (_repair_raise_rate_limit, False, "Raise command rate limit"),
    "db.reset": (_repair_db_reset, True, "Reset database"),
    "google.reset": (_repair_google_reset, True, "Clear Google credentials"),
    "deps.install_spreadsheet": (_repair_install_spreadsheet_deps, False, "Install spreadsheet support"),
    "deps.install_tts": (_repair_install_tts_deps, False, "Install text-to-speech"),
    "deps.install_browser": (_repair_install_browser_deps, False, "Install browser automation"),
    "deps.install_docx": (_repair_install_docx_node, False, "Install Word document support"),
    "service.install_background": (_repair_install_background_service, False, "Install background service"),
    "service.enable_boot": (_repair_enable_service_at_boot, False, "Start automatically at boot"),
    "web.clear_gemini_key": (_repair_clear_gemini_key, True, "Clear rejected Gemini API key"),
    "network.enable_https": (_repair_enable_tailscale_https, False, "Enable HTTPS via Tailscale"),
    "network.reset_https": (_repair_reset_tailscale_https, True, "Reset and re-enable Tailscale HTTPS"),
    "files.reset_scratch": (_repair_clear_scratch, False, "Clear diagnostics scratch files"),
    "tools.reset_dynamic_registry": (_repair_reset_dynamic_registry, True, "Reset custom tool registry"),
    "projects.reset_registry": (_repair_reset_projects_registry, True, "Reset project registry"),
    "system.full_reset": (_repair_full_reset, True, "Deep reset — reconfigure everything"),
}


def list_repairs() -> list[dict]:
    return [
        {"id": key, "label": label, "destructive": destructive}
        for key, (_, destructive, label) in REPAIRS.items()
    ]


async def run_repair(repair_id: str, confirm_destructive: bool = False) -> dict:
    entry = REPAIRS.get(repair_id)
    if not entry:
        return {"ok": False, "message": f"Unknown repair: {repair_id}"}

    handler, destructive, label = entry
    if destructive and not confirm_destructive:
        return {
            "ok": False,
            "needs_confirmation": True,
            "destructive": True,
            "message": f"'{label}' deletes existing configuration and needs explicit confirmation.",
        }

    try:
        result = await handler()
    except Exception as exc:
        return {"ok": False, "message": f"Repair '{label}' failed: {exc}"}

    result.setdefault("repair", repair_id)
    result.setdefault("label", label)
    return result


async def run_auto_repair(confirm_destructive: bool = False) -> dict:
    """Run every non-destructive fix for a currently failing check, then re-test."""
    report = await run_all()
    applied: list[dict] = []
    skipped: list[dict] = []

    seen: set[str] = set()
    for check in report["checks"]:
        if check["status"] not in (FAIL, WARN):
            continue
        repair_id = check.get("fix") or (check.get("reset") if confirm_destructive else None)
        if not repair_id or repair_id in seen:
            if check.get("reset") and not confirm_destructive:
                skipped.append({
                    "check": check["id"],
                    "repair": check["reset"],
                    "reason": "destructive — needs explicit confirmation",
                })
            continue
        seen.add(repair_id)
        outcome = await run_repair(repair_id, confirm_destructive=confirm_destructive)
        applied.append({"check": check["id"], "repair": repair_id, **outcome})

    after = await run_all()
    return {
        "ok": True,
        "applied": applied,
        "skipped": skipped,
        "before": {"overall": report["overall"], "counts": report["counts"]},
        "after": after,
    }
