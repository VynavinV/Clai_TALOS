import os
import platform
import json
from typing import Dict, Any

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_FILE = os.path.join(_SCRIPT_DIR, "terminal_config.json")


def get_system_info() -> Dict[str, Any]:
    return {
        "os": platform.system(),
        "os_version": platform.version(),
        "machine": platform.machine(),
        "hostname": platform.node(),
        "python_version": platform.python_version(),
    }


def get_sandbox_config() -> Dict[str, Any]:
    if os.path.isfile(_CONFIG_FILE):
        with open(_CONFIG_FILE, "r") as f:
            return json.load(f)
    return {"sandbox_mode": "native"}


def get_environment_context() -> str:
    info = get_system_info()
    config = get_sandbox_config()
    sandbox_mode = config.get("sandbox_mode", "native")
    
    os_name = info["os"]
    machine = info["machine"]
    hostname = info["hostname"]
    
    if sandbox_mode == "docker":
        env_desc = f"""You are running in a Docker sandbox container on {hostname}.
- The host machine is {os_name} ({machine})
- You have LIMITED access: commands run in an isolated Alpine Linux container
- No network access, no GUI apps, limited filesystem
- Commands like 'apt-get', 'yum' won't work - use 'apk' instead (Alpine)
- You CANNOT open browsers or GUI applications"""
    elif sandbox_mode == "firejail":
        env_desc = f"""You are running in a Firejail sandbox on {hostname} ({os_name} {machine}).
- You have LIMITED access: commands run in a sandboxed environment
- No network access, restricted filesystem access
- You CANNOT open browsers or GUI applications"""
    else:
        if os_name == "Darwin":
            env_desc = f"""You have FULL access to the user's macOS machine ({hostname}, {machine}).
- You are running directly on their Mac with sudo privileges
- You CAN open GUI applications using: open -a "App Name" or open URL
- Examples: open -a "Google Chrome" https://example.com, open -a "Finder" /path
- You can access files, run scripts, manage processes, install software"""
        elif os_name == "Linux":
            env_desc = f"""You have FULL access to the user's Linux machine ({hostname}, {machine}).
- You are running directly on their system with sudo privileges
- GUI apps: use xdg-open for files/URLs, or run GUI apps directly if X11/Wayland available
- You can access files, run scripts, manage processes, install software"""
        elif os_name == "Windows":
            env_desc = f"""You have FULL access to the user's Windows machine ({hostname}).
- You are running directly on their system
- GUI apps: use start command (e.g., start chrome https://example.com)
- You can access files, run scripts, manage processes"""
        else:
            env_desc = f"""You have FULL access to the user's machine ({hostname}, {os_name} {machine}).
- You are running directly on their system with elevated privileges"""
    
    return env_desc


def get_telegram_formatting_guide() -> str:
    return """## Telegram Formatting Rules

You are responding in a Telegram chat. Follow these rules:
- Use plain text only - no markdown
- For emphasis: use UPPERCASE sparingly, or just plain text
- For lists: use plain dashes (- item) or numbers (1. item) on their own lines
- For code: put it on its own line, optionally with a colon before
- Keep responses concise - Telegram is for quick reading
- No **bold**, *italic*, `code`, or ##headers - they show as raw symbols
- Break long responses into shorter paragraphs with blank lines"""
