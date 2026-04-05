#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

WEB_PORT="${WEB_PORT:-8080}"
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

# ── Step 1: Ensure Python 3.10-3.13 ──────────────────────────────────

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

# ── Step 2: Ensure Tailscale ──────────────────────────────────────────

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

# ── Step 3: Create venv + install deps ────────────────────────────────

setup_venv() {
  local py="$1"

  if [[ -d "venv" ]]; then
    # Validate existing venv
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

  # Install/upgrade deps
  local pip="venv/bin/pip"
  if [[ -f "requirements.txt" ]]; then
    info "Installing dependencies..."
    "$pip" install -q --upgrade pip 2>/dev/null
    "$pip" install -q -r requirements.txt 2>/dev/null
  fi
}

# ── Step 4: Run setup (non-interactive now) ───────────────────────────

run_setup() {
  venv/bin/python setup.py
}

# ── Step 5: Start Tailscale Funnel (best-effort) ─────────────────────

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

# ── Step 6: Open browser ─────────────────────────────────────────────

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

# ══════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════

banner

# Ensure dirs
mkdir -p projects logs/web_uploads logs/browser

# Python
PYTHON=""
PYTHON=$(find_python) || true
if [[ -z "$PYTHON" ]]; then
  install_python
  PYTHON=$(find_python) || { fail "Python installation failed."; exit 1; }
fi
ok "Python: $($PYTHON --version 2>&1)"

# Tailscale (best-effort, non-blocking)
install_tailscale

# Venv + deps
setup_venv "$PYTHON"
ok "Virtual environment ready"

# Non-interactive setup (writes defaults, skips prompts)
run_setup
ok "Configuration checked"

# Tailscale funnel
start_funnel

echo ""
echo -e "${GREEN}${BOLD}  Ready!${RESET}"
echo -e "${DIM}  Dashboard: http://localhost:${WEB_PORT}${RESET}"
echo ""

# Open browser on first run
open_browser

# Start bot
exec venv/bin/python telegram_bot.py
