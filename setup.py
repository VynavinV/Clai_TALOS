#!/usr/bin/env python3
"""
Non-interactive setup: creates defaults, validates environment.
All user-facing configuration happens through the web dashboard onboarding.
"""

import os
import sys
import subprocess
import shutil
import json

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.join(SCRIPT_DIR, "venv")
ENV_FILE = os.path.join(SCRIPT_DIR, ".env")
CREDS_FILE = os.path.join(SCRIPT_DIR, ".credentials")
CONFIG_FILE = os.path.join(SCRIPT_DIR, ".setup_config")
REQUIREMENTS_FILE = os.path.join(SCRIPT_DIR, "requirements.txt")

DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RESET = "\033[0m"

if sys.platform == "win32":
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        DIM = GREEN = YELLOW = RESET = ""


def run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def get_pip():
    if sys.platform == "win32":
        return os.path.join(VENV_DIR, "Scripts", "pip")
    return os.path.join(VENV_DIR, "bin", "pip")


def get_python():
    if sys.platform == "win32":
        return os.path.join(VENV_DIR, "Scripts", "python")
    return os.path.join(VENV_DIR, "bin", "python")


def load_setup_config():
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_setup_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def read_env():
    env = {}
    if os.path.isfile(ENV_FILE):
        with open(ENV_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, val = line.split("=", 1)
                    env[key] = val
    return env


def write_env(env):
    with open(ENV_FILE, "w") as f:
        for key, val in env.items():
            f.write(f"{key}={val}\n")


def _default_chrome_user_data_dir() -> str:
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Google/Chrome")
    if sys.platform.startswith("linux"):
        return os.path.expanduser("~/.config/google-chrome")
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            return os.path.join(local_app_data, "Google", "Chrome", "User Data")
    return ""


def _default_isolated_profile_dir() -> str:
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Clai_TALOS/browser-profile")
    if sys.platform.startswith("linux"):
        return os.path.expanduser("~/.local/share/clai_talos/browser-profile")
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            return os.path.join(local_app_data, "Clai_TALOS", "browser-profile")
    return os.path.join(os.path.expanduser("~"), ".clai_talos-browser-profile")


def ensure_browser_env_defaults(env: dict[str, str]) -> bool:
    defaults = {
        "BROWSER_CDP_ENDPOINT": "http://127.0.0.1:9222",
        "BROWSER_START_IF_NEEDED": "1",
        "BROWSER_AUTO_CONNECT_ON_RUN": "1",
        "BROWSER_ALLOW_ISOLATED_FALLBACK": "0",
        "BROWSER_PROFILE_DIRECTORY": "auto",
        "BROWSER_STARTUP_TIMEOUT_S": "20",
        "BROWSER_ISOLATED_PROFILE_DIR": _default_isolated_profile_dir(),
    }

    chrome_user_data = _default_chrome_user_data_dir()
    if chrome_user_data:
        defaults["BROWSER_CHROME_USER_DATA_DIR"] = chrome_user_data

    changed = False
    for key, value in defaults.items():
        if not env.get(key):
            env[key] = value
            changed = True
    return changed


def load_required_packages() -> list[str]:
    fallback = [
        "python-telegram-bot", "python-dotenv", "aiohttp", "bcrypt",
        "zhipuai", "httpx", "croniter", "google-genai", "openai",
        "anthropic", "gTTS", "playwright", "scrapy", "pandas", "openpyxl",
    ]

    if not os.path.isfile(REQUIREMENTS_FILE):
        return fallback

    out = []
    seen = set()
    with open(REQUIREMENTS_FILE, "r") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "#" in line:
                line = line.split("#", 1)[0].strip()
            if ";" in line:
                line = line.split(";", 1)[0].strip()
            normalized = line
            for sep in ("==", ">=", "<=", "~=", "!=", "<", ">"):
                if sep in normalized:
                    normalized = normalized.split(sep, 1)[0].strip()
                    break
            if "[" in normalized:
                normalized = normalized.split("[", 1)[0].strip()
            if normalized and normalized not in seen:
                out.append(normalized)
                seen.add(normalized)
    return out if out else fallback


def package_is_installed(pip_executable: str, package_name: str) -> bool:
    result = run([pip_executable, "show", package_name])
    return result.returncode == 0


def auto_heal():
    """Non-interactive setup. Returns True if anything was fixed."""
    config = load_setup_config()
    fixed = []

    # Validate existing venv Python version
    if os.path.isdir(VENV_DIR):
        venv_python = get_python()
        if os.path.isfile(venv_python):
            result = run([venv_python, "--version"])
            if result.returncode == 0 and ("3.14" in result.stdout or "3.15" in result.stdout):
                print(f"{YELLOW}[setup] Incompatible Python in venv, recreating...{RESET}")
                shutil.rmtree(VENV_DIR)

    # Install missing packages
    pip = get_pip()
    if os.path.isfile(pip):
        required_packages = load_required_packages()
        missing = [p for p in required_packages if not package_is_installed(pip, p)]
        if missing:
            print(f"{DIM}[setup] Installing {len(missing)} packages...{RESET}")
            result = run([pip, "install", "-q"] + missing)
            if result.returncode != 0:
                print(f"{YELLOW}[setup] Warning: some packages failed to install{RESET}")
            else:
                fixed.append(f"{len(missing)} packages")

    # Ensure .env exists with browser defaults
    env = read_env()
    needs_write = False

    if ensure_browser_env_defaults(env):
        needs_write = True

    # Set BOT_NAME default if missing
    if not env.get("BOT_NAME"):
        env["BOT_NAME"] = "Clai-TALOS"
        needs_write = True

    if needs_write:
        write_env(env)
        fixed.append(".env defaults")

    # Save config
    save_setup_config(config)

    # Ensure directories
    for d in ["projects", "logs", "logs/web_uploads", "logs/browser"]:
        os.makedirs(os.path.join(SCRIPT_DIR, d), exist_ok=True)

    if fixed:
        print(f"{GREEN}[setup] Fixed: {', '.join(fixed)}{RESET}")

    return len(fixed) > 0


if __name__ == "__main__":
    try:
        auto_heal()
    except KeyboardInterrupt:
        print(f"\n{DIM}Setup cancelled.{RESET}")
        sys.exit(1)
