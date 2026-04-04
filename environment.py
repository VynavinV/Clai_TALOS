import os
import platform
import json

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "terminal_config.json")

_env_context: str | None = None


def _build_env_context() -> str:
    config = {}
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                config = json.load(f)
        except Exception:
            pass

    sandbox = config.get("sandbox_mode", "native")
    os_name = platform.system()
    hostname = platform.node()
    machine = platform.machine()

    if sandbox == "docker":
        return (
            f"You are running in a Docker sandbox on {hostname} ({os_name}/{machine}). "
            "Commands run in an isolated Alpine container with no network, no GUI, no persistent state. "
            "Use 'apk' instead of apt/yum."
        )
    if sandbox == "firejail":
        return (
            f"You are running in a Firejail sandbox on {hostname} ({os_name}/{machine}). "
            "No network, restricted filesystem, no GUI."
        )

    if os_name == "Darwin":
        return (
            f"You have FULL access to the user's Mac ({hostname}, {machine}). "
            "You CAN open apps: open -a 'App Name' or open URL. "
            "You can access files, run scripts, manage processes, install software."
        )
    if os_name == "Linux":
        return (
            f"You have FULL access to the user's Linux machine ({hostname}, {machine}). "
            "GUI apps: use xdg-open. You can access files, run scripts, manage processes."
        )
    if os_name == "Windows":
        return (
            f"You have FULL access to the user's Windows machine ({hostname}, {machine}). "
            "You CAN open apps/URLs with 'start'. "
            "Use cmd.exe commands. You can access files, run scripts, manage processes."
        )
    return f"You have FULL access to the user's machine ({hostname}, {os_name}/{machine})."


def get_environment_context() -> str:
    global _env_context
    if _env_context is None:
        _env_context = _build_env_context()
    return _env_context


def get_telegram_formatting_guide() -> str:
    return (
        "You are responding in Telegram. Use plain text only - no markdown. "
        "No **bold**, *italic*, `code`, or ## headers. "
        "Use dashes for lists. Keep responses concise."
    )
