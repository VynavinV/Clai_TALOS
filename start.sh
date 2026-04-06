#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/src"

HEADLESS=false
for arg in "$@"; do
  case "$arg" in
    --headless) HEADLESS=true ;;
  esac
done

WEB_PORT="${WEB_PORT:-8080}"
MIN_DASH_PASSWORD_LENGTH=10
BOLD="\033[1m"
CYAN="\033[36m"
GREEN="\033[32m"
YELLOW="\033[33m"
DIM="\033[2m"
RED="\033[31m"
RESET="\033[0m"

banner() {
  echo ""
  echo -e "${CYAN}${BOLD}  ╔══════════════════════════════════╗${RESET}"
  echo -e "${CYAN}${BOLD}  ║         Clai  TALOS              ║${RESET}"
  echo -e "${CYAN}${BOLD}  ║    Personal AI Assistant          ║${RESET}"
  echo -e "${CYAN}${BOLD}  ╚══════════════════════════════════╝${RESET}"
  echo ""
}

info()  { echo -e "${DIM}[setup]${RESET} $1"; }
ok()    { echo -e "${GREEN}[  ok ]${RESET} $1"; }
warn()  { echo -e "${YELLOW}[ warn]${RESET} $1"; }
fail()  { echo -e "${RED}[fail ]${RESET} $1"; }

prompt() {
  local var="$1"
  local msg="$2"
  local default="$3"
  local val
  if [[ -n "$default" ]]; then
    echo -ne "${DIM}${msg} [${default}]: ${RESET}"
  else
    echo -ne "${DIM}${msg}: ${RESET}"
  fi
  read -r val
  val="${val:-$default}"
  eval "$var=\"\$val\""
}

prompt_secret() {
  local var="$1"
  local msg="$2"
  local val
  echo -ne "${DIM}${msg}: ${RESET}"
  read -rs val
  echo ""
  eval "$var=\"\$val\""
}

# ── Step 1: Ensure sudo access ──────────────────────────────────────

ensure_sudo() {
  if [[ "$(uname)" == "Darwin" ]]; then
    return 0
  fi
  if [[ "$EUID" -eq 0 ]]; then
    return 0
  fi
  local sudoers_file="/etc/sudoers.d/clai-talos"
  local current_user
  current_user="$(whoami)"
  if sudo -n true 2>/dev/null; then
    ok "Sudo access available"
    return 0
  fi
  info "Configuring passwordless sudo for TALOS..."
  echo "$current_user" | sudo -S bash -c "echo '$current_user ALL=(ALL) NOPASSWD: ALL' > '$sudoers_file' && chmod 440 '$sudoers_file'" 2>/dev/null
  if sudo -n true 2>/dev/null; then
    ok "Sudo access configured"
  else
    warn "Could not configure passwordless sudo. Some features may require manual sudo."
  fi
}

# ── Step 2: Ensure Python 3.10-3.13 ──────────────────────────────────

find_python() {
  for cmd in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$cmd" &>/dev/null; then
      local ver
      ver=$("$cmd" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+')
      case "$ver" in
        3.10|3.11|3.12|3.13) echo "$cmd"; return 0 ;;
      esac
    fi
  done
  return 1
}

install_python() {
  info "Python 3.10-3.13 not found. Attempting install..."
  if [[ "$(uname)" == "Darwin" ]]; then
    if command -v brew &>/dev/null; then
      info "Installing Python 3.13 via Homebrew..."
      brew install python@3.13
    else
      fail "Homebrew not found. Install Python 3.10-3.13 manually:"
      fail "  https://www.python.org/downloads/"
      exit 1
    fi
  elif command -v apt-get &>/dev/null; then
    info "Installing Python 3.12 via apt..."
    sudo apt-get update -qq && sudo apt-get install -y -qq python3.12 python3.12-venv python3-pip
  elif command -v dnf &>/dev/null; then
    info "Installing Python 3.12 via dnf..."
    sudo dnf install -y python3.12
  elif command -v pacman &>/dev/null; then
    info "Installing Python via pacman..."
    sudo pacman -Sy --noconfirm python
  else
    fail "Could not auto-install Python. Install Python 3.10-3.13 manually:"
    fail "  https://www.python.org/downloads/"
    exit 1
  fi
}

# ── Step 3: Ensure Tailscale ──────────────────────────────────────────

install_tailscale() {
  if command -v tailscale &>/dev/null; then
    return 0
  fi
  info "Installing Tailscale..."
  if [[ "$(uname)" == "Darwin" ]]; then
    if command -v brew &>/dev/null; then
      brew install --cask tailscale 2>/dev/null || brew install tailscale 2>/dev/null || true
    else
      warn "Install Tailscale from: https://tailscale.com/download/mac"
      warn "You can do this later from the dashboard."
    fi
  elif command -v curl &>/dev/null; then
    curl -fsSL https://tailscale.com/install.sh | sh || {
      warn "Tailscale auto-install failed."
      warn "Install from: https://tailscale.com/download"
      warn "You can do this later from the dashboard."
    }
  else
    warn "Install Tailscale from: https://tailscale.com/download"
    warn "You can do this later from the dashboard."
  fi
}

# ── Step 4: Create venv + install deps ────────────────────────────────

setup_venv() {
  local py="$1"

  if [[ -d "venv" ]]; then
    local venv_py="venv/bin/python"
    if [[ -f "$venv_py" ]]; then
      local ver
      ver=$("$venv_py" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' || echo "")
      case "$ver" in
        3.10|3.11|3.12|3.13) ;;
        *) info "Recreating venv (incompatible Python)..."; rm -rf venv ;;
      esac
    else
      rm -rf venv
    fi
  fi

  if [[ ! -d "venv" ]]; then
    info "Creating virtual environment..."
    "$py" -m venv venv
  fi

  local pip="venv/bin/pip"
  if [[ -f "requirements.txt" ]]; then
    info "Installing dependencies..."
    "$pip" install -q --upgrade pip 2>/dev/null
    "$pip" install -q -r requirements.txt 2>/dev/null
  fi
}

# ── Step 5: Run setup (non-interactive now) ───────────────────────────

run_setup() {
  venv/bin/python setup.py
}

# ── Step 6: Start Tailscale Funnel (best-effort) ─────────────────────

start_funnel() {
  if ! command -v tailscale &>/dev/null; then
    return 0
  fi
  if tailscale status &>/dev/null; then
    if ! tailscale funnel status 2>/dev/null | grep -q "$WEB_PORT"; then
      tailscale funnel --bg "$WEB_PORT" 2>/dev/null && ok "Tailscale Funnel active" || true
    fi
  fi
}

# ── Step 7: Open browser ─────────────────────────────────────────────

open_browser() {
  local url="http://localhost:${WEB_PORT}"
  info "Opening dashboard at ${url}"
  if [[ "$(uname)" == "Darwin" ]]; then
    open "$url" 2>/dev/null &
  elif command -v xdg-open &>/dev/null; then
    xdg-open "$url" 2>/dev/null &
  elif command -v wslview &>/dev/null; then
    wslview "$url" 2>/dev/null &
  fi
}

# ── Headless helpers ─────────────────────────────────────────────────

needs_onboarding() {
  if [[ ! -f .env ]]; then
    return 0
  fi
  grep -q '^TELEGRAM_BOT_TOKEN=' .env 2>/dev/null || return 0
  local token
  token=$(grep '^TELEGRAM_BOT_TOKEN=' .env | cut -d'=' -f2-)
  [[ -z "$token" || "$token" == "your_telegram_bot_token" ]]
}

has_credentials() {
  [[ -f .credentials ]]
}

env_get() {
  local key="$1"
  if [[ -f .env ]]; then
    grep "^${key}=" .env 2>/dev/null | head -1 | cut -d'=' -f2-
  fi
}

env_set() {
  local key="$1"
  local val="$2"
  if [[ -f .env ]] && grep -q "^${key}=" .env 2>/dev/null; then
    local tmp
    tmp=$(mktemp)
    sed "s|^${key}=.*|${key}=${val}|" .env > "$tmp"
    mv "$tmp" .env
  else
    echo "${key}=${val}" >> .env
  fi
}

create_credentials() {
  local username="$1"
  local password="$2"
  local hash
  hash=$(DASH_PASSWORD_RAW="$password" venv/bin/python - <<'PY'
import os
import bcrypt

raw = os.environ.get("DASH_PASSWORD_RAW", "")
print(bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8"))
PY
)
  echo "USERNAME=${username}" > .credentials
  echo "PASSWORD_HASH=${hash}" >> .credentials
  chmod 600 .credentials
}

get_tailscale_url() {
  if command -v tailscale &>/dev/null && tailscale status &>/dev/null; then
    local dns_name
    dns_name=$(tailscale status --json 2>/dev/null | venv/bin/python -c "import sys,json; d=json.load(sys.stdin); print(d.get('Self',{}).get('DNSName','').rstrip('.'))" 2>/dev/null || true)
    if [[ -n "$dns_name" ]]; then
      if tailscale funnel status 2>/dev/null | grep -q "$WEB_PORT"; then
        echo "https://${dns_name}"
        return
      fi
      echo "http://${dns_name}:${WEB_PORT}"
      return
    fi
  fi
  echo "http://localhost:${WEB_PORT}"
}

# ── Headless: Tailscale + browser mode ──────────────────────────────

headless_tailscale_mode() {
  echo ""
  echo -e "${CYAN}${BOLD}  ── Tailscale Remote Setup ──${RESET}"
  echo ""

  if ! command -v tailscale &>/dev/null; then
    fail "Tailscale is not installed."
    info "Install it with: curl -fsSL https://tailscale.com/install.sh | sh"
    info "Then re-run: ./start.sh --headless"
    exit 1
  fi

  if ! tailscale status &>/dev/null; then
    info "Tailscale is installed but not connected. Starting..."
    echo ""
    info "Run this command in another terminal if it needs interaction:"
    echo -e "${BOLD}    sudo tailscale up${RESET}"
    echo ""
    info "Waiting for Tailscale connection..."
    local tries=0
    while ! tailscale status &>/dev/null; do
      sleep 2
      tries=$((tries + 1))
      if [[ $tries -gt 60 ]]; then
        fail "Timed out waiting for Tailscale. Run 'sudo tailscale up' manually and retry."
        exit 1
      fi
    done
    ok "Tailscale connected"
  else
    ok "Tailscale is connected"
  fi

  if ! has_credentials; then
    echo ""
    echo -e "${DIM}Create a dashboard account (used to log in from your browser):${RESET}"
    prompt DASH_USER "Username" "admin"
    prompt_secret DASH_PASS "Password (min ${MIN_DASH_PASSWORD_LENGTH} chars)"
    while [[ ${#DASH_PASS} -lt ${MIN_DASH_PASSWORD_LENGTH} ]]; do
      warn "Password must be at least ${MIN_DASH_PASSWORD_LENGTH} characters."
      prompt_secret DASH_PASS "Password (min ${MIN_DASH_PASSWORD_LENGTH} chars)"
    done
    create_credentials "$DASH_USER" "$DASH_PASS"
    ok "Dashboard account created"
  else
    ok "Dashboard account exists"
  fi

  echo ""
  info "Starting Tailscale Funnel on port ${WEB_PORT}..."
  if ! tailscale funnel status 2>/dev/null | grep -q "$WEB_PORT"; then
    tailscale funnel --bg "$WEB_PORT" 2>/dev/null && ok "Tailscale Funnel active" || {
      warn "Could not start funnel. Trying tailscale serve..."
      tailscale serve --bg "$WEB_PORT" 2>/dev/null && ok "Tailscale Serve active" || {
        fail "Could not expose port via Tailscale."
        info "Make sure funnel is enabled: https://tailscale.com/kb/1223/funnel"
        exit 1
      }
    }
  else
    ok "Tailscale Funnel already active"
  fi

  local url
  url=$(get_tailscale_url)

  echo ""
  echo -e "${GREEN}${BOLD}  ─────────────────────────────────────${RESET}"
  echo -e "${GREEN}${BOLD}  Dashboard URL:${RESET}"
  echo -e "${BOLD}  ${url}${RESET}"
  echo -e "${GREEN}${BOLD}  ─────────────────────────────────────${RESET}"
  echo ""
  info "Open that URL on any device to complete setup."
  info "The onboarding wizard will guide you through the rest."
  echo ""
}

# ── Headless: Terminal setup wizard ─────────────────────────────────

headless_terminal_setup() {
  echo ""
  echo -e "${CYAN}${BOLD}  ── Terminal Setup Wizard ──${RESET}"
  echo ""

  # ── Telegram ──
  echo -e "${BOLD}Step 1: Connect Telegram${RESET}"
  echo -e "${DIM}Get a bot token from @BotFather on Telegram (/newbot)${RESET}"
  echo ""
  prompt TG_TOKEN "Telegram Bot Token" ""
  while [[ -z "$TG_TOKEN" ]]; do
    warn "Token is required."
    prompt TG_TOKEN "Telegram Bot Token" ""
  done
  prompt BOT_NAME "Bot Name" "Clai-TALOS"
  env_set "TELEGRAM_BOT_TOKEN" "$TG_TOKEN"
  env_set "BOT_NAME" "$BOT_NAME"
  ok "Telegram configured"

  # ── AI Provider ──
  echo ""
  echo -e "${BOLD}Step 2: Choose AI Provider${RESET}"
  echo ""
  echo -e "  ${DIM}1) OpenAI        (gpt-4o, o3, o4-mini)${RESET}"
  echo -e "  ${DIM}2) Anthropic     (claude-sonnet-4, claude-3.5-sonnet)${RESET}"
  echo -e "  ${DIM}3) Gemini        (gemini-2.5-pro, gemini-2.0-flash)${RESET}"
  echo -e "  ${DIM}4) ZhipuAI       (glm-5, glm-4v)${RESET}"
  echo -e "  ${DIM}5) NVIDIA        (glm4.7)${RESET}"
  echo -e "  ${DIM}6) Cerebras      (llama4-scout, llama-3.3-70b)${RESET}"
  echo -e "  ${DIM}7) OpenRouter    (200+ models via one key)${RESET}"
  echo -e "  ${DIM}8) Ollama        (local models, no key needed)${RESET}"
  echo ""
  prompt PROVIDER_NUM "Provider [1-8]" "2"

  local provider="" env_key="" default_model=""
  case "$PROVIDER_NUM" in
    1) provider="openai";    env_key="OPENAI_API_KEY";    default_model="openai/gpt-4o" ;;
    2) provider="anthropic"; env_key="ANTHROPIC_API_KEY"; default_model="anthropic/claude-sonnet-4-20250514" ;;
    3) provider="gemini";    env_key="GEMINI_API_KEY";    default_model="gemini/gemini-2.5-pro" ;;
    4) provider="zhipu";     env_key="ZHIPUAI_API_KEY";   default_model="zhipu/glm-5" ;;
    5) provider="nvidia";    env_key="NVIDIA_API_KEY";    default_model="nvidia/z-ai/glm4.7" ;;
    6) provider="cerebras";  env_key="CEREBRAS_API_KEY";  default_model="cerebras/llama4-scout-17b-16e-instruct" ;;
    7) provider="openrouter";env_key="OPENROUTER_API_KEY"; default_model="openrouter/anthropic/claude-sonnet-4-20250514" ;;
    8) provider="ollama";    env_key="";                   default_model="" ;;
    *) fail "Invalid choice. Defaulting to Anthropic."; provider="anthropic"; env_key="ANTHROPIC_API_KEY"; default_model="anthropic/claude-sonnet-4-20250514" ;;
  esac

  if [[ "$provider" == "ollama" ]]; then
    echo ""
    echo -e "${DIM}Ollama runs models locally. Make sure Ollama is installed and running.${RESET}"
    echo -e "${DIM}Install from: https://ollama.com${RESET}"
    echo ""
    prompt OLLAMA_MODEL "Model name (e.g. llama3, mistral, deepseek-r1)" "llama3"
    env_set "OLLAMA_MODEL" "$OLLAMA_MODEL"
    env_set "MAIN_MODEL" "ollama/${OLLAMA_MODEL}"
    ok "Ollama configured: ${OLLAMA_MODEL}"
  else
    echo ""
    prompt_secret API_KEY "${provider^} API Key"
    while [[ -z "$API_KEY" ]]; do
      warn "API key is required."
      prompt_secret API_KEY "${provider^} API Key"
    done
    env_set "$env_key" "$API_KEY"

    echo ""
    prompt MAIN_MODEL "Main model" "$default_model"
    env_set "MAIN_MODEL" "$MAIN_MODEL"
    ok "AI provider configured: ${provider}/${MAIN_MODEL#*/}"
  fi

  # ── Optional: Gemini for web search ──
  echo ""
  echo -e "${BOLD}Step 3: Web Search (Optional)${RESET}"
  echo -e "${DIM}A Gemini API key enables web search.${RESET}"
  if [[ "$provider" != "gemini" ]]; then
    prompt GEMINI_KEY "Gemini API Key (press Enter to skip)" ""
    if [[ -n "$GEMINI_KEY" ]]; then
      env_set "GEMINI_API_KEY" "$GEMINI_KEY"
      ok "Web search enabled"
    else
      info "Web search skipped"
    fi
  else
    ok "Web search already enabled (Gemini key set above)"
  fi

  # ── Dashboard credentials ──
  echo ""
  if ! has_credentials; then
    echo -e "${BOLD}Step 4: Dashboard Account${RESET}"
    echo -e "${DIM}Create credentials for the web dashboard (optional but recommended).${RESET}"
    prompt CREATE_CREDS "Create dashboard account? [y/N]" "n"
    if [[ "$CREATE_CREDS" =~ ^[Yy]$ ]]; then
      prompt DASH_USER "Username" "admin"
      prompt_secret DASH_PASS "Password (min ${MIN_DASH_PASSWORD_LENGTH} chars)"
      while [[ ${#DASH_PASS} -lt ${MIN_DASH_PASSWORD_LENGTH} ]]; do
        warn "Password must be at least ${MIN_DASH_PASSWORD_LENGTH} characters."
        prompt_secret DASH_PASS "Password (min ${MIN_DASH_PASSWORD_LENGTH} chars)"
      done
      create_credentials "$DASH_USER" "$DASH_PASS"
      ok "Dashboard account created"
    else
      info "Dashboard account skipped (you can create it later via the web UI)"
    fi
  else
    ok "Dashboard account exists"
  fi

  echo ""
  echo -e "${GREEN}${BOLD}  ─────────────────────────────────────${RESET}"
  echo -e "${GREEN}${BOLD}  Setup complete!${RESET}"
  echo -e "${GREEN}${BOLD}  ─────────────────────────────────────${RESET}"
  echo ""
}

# ══════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════

banner

mkdir -p projects logs/web_uploads logs/browser bin

ensure_sudo

PYTHON=""
PYTHON=$(find_python) || true
if [[ -z "$PYTHON" ]]; then
  install_python
  PYTHON=$(find_python) || { fail "Python installation failed."; exit 1; }
fi
ok "Python: $($PYTHON --version 2>&1)"

install_tailscale

setup_venv "$PYTHON"
ok "Virtual environment ready"

run_setup
ok "Configuration checked"

if [[ "$HEADLESS" == true ]]; then
  if needs_onboarding; then
    echo ""
    echo -e "${BOLD}  No configuration found. Choose a setup method:${RESET}"
    echo ""
    echo -e "  ${CYAN}1)${RESET} Tailscale + browser  (configure from another device)"
    echo -e "  ${CYAN}2)${RESET} Terminal setup        (enter keys here)"
    echo ""
    prompt SETUP_CHOICE "Choice [1/2]" "1"

    case "$SETUP_CHOICE" in
      1) headless_tailscale_mode ;;
      2) headless_terminal_setup ;;
      *) fail "Invalid choice."; exit 1 ;;
    esac
  else
    ok "Configuration found, starting..."
  fi

  start_funnel

  echo -e "${GREEN}${BOLD}  Starting...${RESET}"
  echo ""
  exec venv/bin/python telegram_bot.py
else
  start_funnel

  echo ""
  echo -e "${GREEN}${BOLD}  Ready!${RESET}"
  echo -e "${DIM}  Dashboard: http://localhost:${WEB_PORT}${RESET}"
  echo ""

  open_browser

  exec venv/bin/python telegram_bot.py
fi
