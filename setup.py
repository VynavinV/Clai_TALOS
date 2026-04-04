#!/usr/bin/env python3

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

REQUIRED_PACKAGES = [
    "python-telegram-bot",
    "python-dotenv",
    "aiohttp",
    "bcrypt",
    "zhipuai",
    "httpx",
    "croniter",
    "google-genai",
    "gTTS",
]

BOLD = "\033[1m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"

if sys.platform == "win32":
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        STD_OUTPUT_HANDLE = -11
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        BOLD = CYAN = GREEN = YELLOW = DIM = RESET = ""

def run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)

def check_python():
    if sys.platform == "win32":
        candidates = ["py", "python"]
    else:
        candidates = ["python3.13", "python3.12", "python3.11", "python3.10", "python3"]
    for candidate in candidates:
        if shutil.which(candidate):
            result = run([candidate, "--version"])
            if result.returncode == 0:
                version_str = result.stdout.strip()
                if "3.14" in version_str or "3.15" in version_str:
                    continue
                return candidate
    return None

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

def ask_input(prompt, default="", optional=False):
    if optional:
        prompt_text = f"{CYAN}{BOLD}  → {prompt} (optional, press Enter to skip):{RESET} "
    else:
        prompt_text = f"{CYAN}{BOLD}  → {prompt}:{RESET} "
    
    if default:
        prompt_text = f"{CYAN}{BOLD}  → {prompt} [{default}]:{RESET} "
    
    value = input(prompt_text).strip()
    
    if not value and default:
        return default
    if not value and optional:
        return ""
    if not value and not optional:
        print(f"{YELLOW}  ⚠  This field is required{RESET}")
        return ask_input(prompt, default, optional)
    
    return value

def ask_yes_no(prompt, default="y"):
    default_display = "Y/n" if default == "y" else "y/N"
    while True:
        response = input(f"{DIM}      {prompt} ({default_display}): {RESET}").strip().lower()
        if not response:
            return default == "y"
        if response in ["y", "yes"]:
            return True
        if response in ["n", "no"]:
            return False
        print(f"{YELLOW}  ⚠  Please enter y or n{RESET}")

def auto_heal():
    config = load_setup_config()
    fixed = []
    
    # Check existing venv Python version
    if os.path.isdir(VENV_DIR):
        venv_python = get_python()
        if os.path.isfile(venv_python):
            result = run([venv_python, "--version"])
            if result.returncode == 0 and ("3.14" in result.stdout or "3.15" in result.stdout):
                print(f"{YELLOW}[setup] Python 3.14+ has compatibility issues with zhipuai{RESET}")
                print(f"{DIM}[setup] Recreating venv with Python 3.13...{RESET}")
                shutil.rmtree(VENV_DIR)
    
    # Create venv
    if not os.path.isdir(VENV_DIR):
        python = check_python()
        if not python:
            print(f"\n{YELLOW}ERROR: Python 3.10-3.13 required (3.14+ has zhipuai compatibility issues){RESET}")
            sys.exit(1)
        print(f"{DIM}[setup] Creating virtual environment...{RESET}")
        result = run([python, "-m", "venv", VENV_DIR])
        if result.returncode != 0:
            print(f"{YELLOW}ERROR: Failed to create venv{RESET}")
            print(result.stderr)
            sys.exit(1)
        fixed.append("venv")
    
    # Install missing packages
    pip = get_pip()
    python = get_python()
    
    if not os.path.isfile(pip):
        print(f"{YELLOW}ERROR: pip not found in venv{RESET}")
        sys.exit(1)
    
    missing = []
    for package in REQUIRED_PACKAGES:
        module_name = package.replace("-", "_")
        result = run([python, "-c", f"import {module_name}"])
        if result.returncode != 0:
            missing.append(package)
    
    if missing:
        print(f"{DIM}[setup] Installing {len(missing)} packages...{RESET}")
        result = run([pip, "install", "-q"] + missing)
        if result.returncode != 0:
            print(f"{YELLOW}ERROR: Failed to install packages{RESET}")
            print(result.stderr)
            sys.exit(1)
        fixed.append(f"{len(missing)} packages")
    
    # Check .env file
    env = read_env()
    needs_setup = False
    
    # Required: TELEGRAM_BOT_TOKEN
    if not env.get("TELEGRAM_BOT_TOKEN"):
        if not config.get("asked_telegram"):
            print(f"\n{CYAN}{BOLD}{'─'*50}{RESET}")
            print(f"{CYAN}{BOLD}  Telegram Bot Setup{RESET}")
            print(f"{CYAN}{BOLD}{'─'*50}{RESET}\n")
            print(f"{DIM}How to get your bot token:{RESET}")
            print(f"{DIM}  1. Open Telegram and search for @BotFather{RESET}")
            print(f"{DIM}  2. Send /newbot and follow the prompts{RESET}")
            print(f"{DIM}  3. Copy the token BotFather gives you{RESET}\n")
            
            token = ask_input("Paste your Telegram bot token")
            env["TELEGRAM_BOT_TOKEN"] = token
            config["asked_telegram"] = True
            needs_setup = True
    
    # Required: BOT_NAME
    if not env.get("BOT_NAME"):
        if not config.get("asked_bot_name"):
            name = ask_input("What should this bot be called?", default="Clai-TALOS")
            env["BOT_NAME"] = name
            config["asked_bot_name"] = True
            needs_setup = True
    
    # Optional: ZHIPUAI_API_KEY
    if not env.get("ZHIPUAI_API_KEY"):
        if not config.get("skip_zhipuai") and not config.get("asked_zhipuai"):
            print(f"\n{CYAN}{BOLD}{'─'*50}{RESET}")
            print(f"{CYAN}{BOLD}  API Keys (Optional){RESET}")
            print(f"{CYAN}{BOLD}{'─'*50}{RESET}\n")
            
            print(f"{DIM}ZhipuAI provides GLM-4 models (recommended){RESET}")
            print(f"{DIM}Get key: https://open.bigmodel.cn/{RESET}\n")
            
            key = ask_input("ZhipuAI API key", optional=True)
            if key:
                env["ZHIPUAI_API_KEY"] = key
                config["asked_zhipuai"] = True
                needs_setup = True
            else:
                if ask_yes_no("Never ask about ZhipuAI again?"):
                    config["skip_zhipuai"] = True
                config["asked_zhipuai"] = True
    
    # Optional: GEMINI_API_KEY
    if not env.get("GEMINI_API_KEY"):
        if not config.get("skip_gemini") and not config.get("asked_gemini"):
            print(f"\n{DIM}Gemini provides Google's AI models{RESET}")
            print(f"{DIM}Get key: https://aistudio.google.com/app/apikey{RESET}\n")
            
            key = ask_input("Gemini API key", optional=True)
            if key:
                env["GEMINI_API_KEY"] = key
                config["asked_gemini"] = True
                needs_setup = True
            else:
                if ask_yes_no("Never ask about Gemini again?"):
                    config["skip_gemini"] = True
                config["asked_gemini"] = True
    
    # Optional: FIRECRAWL_API_KEY
    if not env.get("FIRECRAWL_API_KEY"):
        if not config.get("skip_firecrawl") and not config.get("asked_firecrawl"):
            print(f"\n{DIM}Firecrawl for web scraping{RESET}")
            print(f"{DIM}Get key: https://www.firecrawl.dev/app{RESET}\n")
            
            key = ask_input("Firecrawl API key", optional=True)
            if key:
                env["FIRECRAWL_API_KEY"] = key
                config["asked_firecrawl"] = True
                needs_setup = True
            else:
                if ask_yes_no("Never ask about Firecrawl again?"):
                    config["skip_firecrawl"] = True
                config["asked_firecrawl"] = True
    
    # Save .env if changed
    if needs_setup:
        write_env(env)
        fixed.append(".env")
    
    # Save config
    save_setup_config(config)
    
    # Create default credentials if missing
    if not os.path.isfile(CREDS_FILE):
        with open(CREDS_FILE, "w") as f:
            f.write("USERNAME=admin\n")
            f.write("PASSWORD=admin\n")
        if sys.platform != "win32":
            os.chmod(CREDS_FILE, 0o600)
        fixed.append("credentials (admin/admin)")
        print(f"{DIM}[setup] Created default credentials (admin/admin){RESET}")
        print(f"{DIM}[setup] Change via dashboard: http://localhost:8080{RESET}")
    
    if fixed:
        print(f"{GREEN}[setup] ✓ Auto-fixed: {', '.join(fixed)}{RESET}")
    
    return len(fixed) > 0

if __name__ == "__main__":
    try:
        auto_heal()
    except KeyboardInterrupt:
        print(f"\n{DIM}Setup cancelled.{RESET}")
        sys.exit(1)
