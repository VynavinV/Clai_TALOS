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

# Make sure tailscaled itself is up. A fresh apt install leaves the unit
# enabled but not always started, and every later step fails confusingly if
# the daemon is not listening.
ensure_tailscaled_running() {
  command -v tailscale &>/dev/null || return 1
  tailscale status &>/dev/null && return 0
  # "Logged out"/"stopped" still means the daemon answered — that is fine here.
  tailscale status 2>&1 | grep -qiE "logged out|stopped|NeedsLogin" && return 0

  if command -v systemctl &>/dev/null; then
    info "Starting the tailscaled service..."
    sudo systemctl enable --now tailscaled 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      tailscale status &>/dev/null && return 0
      tailscale status 2>&1 | grep -qiE "logged out|stopped|NeedsLogin" && return 0
      sleep 1
    done
  fi
  return 1
}

# Warn about the failure mode that produces a perfect-looking config which
# serves nothing: two daemons, one holding the tailnet IP, the other holding
# the serve config.
warn_duplicate_tailscaled() {
  local count
  count=$(ps ax -o command 2>/dev/null \
          | grep -icE "tailscaled|IPNExtension" || true)
  if [[ "${count:-0}" -gt 1 ]]; then
    warn "More than one Tailscale daemon appears to be running."
    warn "Keep a single installation, or HTTPS may be configured on the wrong one."
  fi
}

# Bring the node onto the tailnet. Non-interactive when TS_AUTHKEY is set,
# otherwise surfaces the login URL here instead of telling the user to open a
# second terminal and run `tailscale up` themselves.
ensure_tailscale_up() {
  command -v tailscale &>/dev/null || return 1
  if tailscale status &>/dev/null; then
    return 0
  fi

  if [[ -n "${TS_AUTHKEY:-}" ]]; then
    info "Authenticating Tailscale with TS_AUTHKEY..."
    sudo tailscale up --authkey "$TS_AUTHKEY" --hostname "$(hostname -s 2>/dev/null || hostname)" \
      && { ok "Tailscale connected"; return 0; }
    warn "Auth key was rejected; falling back to browser login."
  fi

  info "Connecting this machine to your tailnet..."
  local up_log
  up_log=$(mktemp)
  # `tailscale up` blocks while waiting for the browser login, so run it in the
  # background and read the auth URL out of its output.
  ( sudo tailscale up --hostname "$(hostname -s 2>/dev/null || hostname)" >"$up_log" 2>&1 ) &
  local up_pid=$!

  local url="" waited=0
  while [[ $waited -lt 180 ]]; do
    if tailscale status &>/dev/null; then
      wait $up_pid 2>/dev/null || true
      rm -f "$up_log"
      ok "Tailscale connected"
      return 0
    fi
    if [[ -z "$url" ]]; then
      url=$(grep -oE 'https://login\.tailscale\.com/[a-zA-Z0-9/_-]+' "$up_log" 2>/dev/null | head -1 || true)
      if [[ -n "$url" ]]; then
        echo ""
        echo -e "${BOLD}  Open this link to connect this machine to your tailnet:${RESET}"
        echo -e "${CYAN}${BOLD}    ${url}${RESET}"
        echo ""
        info "Waiting for you to finish signing in..."
      fi
    fi
    sleep 2
    waited=$((waited + 2))
  done

  kill $up_pid 2>/dev/null || true
  fail "Timed out waiting for Tailscale sign-in."
  [[ -s "$up_log" ]] && { echo "  Tailscale said:"; sed 's/^/    /' "$up_log" | head -5; }
  rm -f "$up_log"
  return 1
}

# One call that takes Tailscale from "maybe installed" to "HTTPS is serving the
# dashboard", reporting precisely which step failed if any does.
setup_tailscale_full() {
  local want_mode="${1:-funnel}"

  install_tailscale
  command -v tailscale &>/dev/null || { warn "Tailscale unavailable; continuing on HTTP."; return 1; }

  ensure_tailscaled_running || { warn "tailscaled is not running; continuing on HTTP."; return 1; }
  ensure_tailscale_up || return 1
  warn_duplicate_tailscaled
  ensure_tailscale_operator || true

  local err
  if [[ "$want_mode" == "funnel" ]]; then
    err=$(tailscale_expose funnel) && { ok "HTTPS active — public (Funnel)"; return 0; }
    warn "Funnel unavailable: ${err%%$'\n'*}"
    info "Falling back to tailnet-only HTTPS..."
  fi

  err=$(tailscale_expose serve) && { ok "HTTPS active — tailnet only (Serve)"; return 0; }

  fail "Could not expose port ${WEB_PORT} over HTTPS."
  echo ""
  echo -e "${BOLD}  Tailscale said:${RESET}"
  echo "    ${err}"
  echo ""
  echo -e "${BOLD}  Most likely fixes:${RESET}"
  echo "    1. Allow this account to configure Tailscale (one time):"
  echo -e "         ${BOLD}sudo tailscale set --operator=${USER}${RESET}"
  echo "    2. Enable HTTPS certificates for your tailnet:"
  echo "         https://login.tailscale.com/admin/dns  (DNS -> HTTPS Certificates)"
  echo "    3. For public access, enable Funnel in your ACLs:"
  echo "         https://tailscale.com/kb/1223/funnel"
  echo ""
  return 1
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

# Tailscale refuses to write serve config unless the caller is root or the
# registered operator. On a fresh Linux install that means the very first
# `funnel`/`serve` call is denied — which used to surface as a generic
# "could not expose port" because stderr was thrown away. Register the operator
# once (what Tailscale itself recommends) and retry.
ensure_tailscale_operator() {
  local err
  err=$(tailscale serve status 2>&1 >/dev/null) || true
  if ! echo "$err" | grep -qiE "access denied|serve config denied|operator"; then
    return 0
  fi

  info "Tailscale needs one-time permission for this account..."
  if sudo -n true 2>/dev/null; then
    sudo -n tailscale set --operator="$USER" 2>/dev/null && {
      ok "Registered ${USER} as Tailscale operator"
      return 0
    }
  fi
  if sudo tailscale set --operator="$USER"; then
    ok "Registered ${USER} as Tailscale operator"
    return 0
  fi
  warn "Could not register the Tailscale operator automatically."
  return 1
}

# Bring up HTTPS. $1 = mode (funnel|serve). Echoes the real error on failure.
tailscale_expose() {
  local mode="$1" out
  if tailscale "$mode" status 2>/dev/null | grep -q "$WEB_PORT"; then
    return 0
  fi
  out=$(tailscale "$mode" --bg "$WEB_PORT" 2>&1) && return 0

  if echo "$out" | grep -qiE "access denied|serve config denied|operator"; then
    ensure_tailscale_operator || { echo "$out"; return 1; }
    out=$(tailscale "$mode" --bg "$WEB_PORT" 2>&1) && return 0
  fi

  echo "$out"
  return 1
}

start_funnel() {
  if ! command -v tailscale &>/dev/null; then
    return 0
  fi
  if tailscale status &>/dev/null; then
    tailscale_expose funnel >/dev/null 2>&1 && ok "Tailscale Funnel active" || true
  fi
}

# ── Background service + `clai` command ──────────────────────────────
#
# Over SSH the assistant has to outlive the session, which previously meant the
# user wiring up systemd themselves. These install a real service plus a `clai`
# command so the whole thing is one prompt.

SERVICE_NAME="clai-talos"
CLAI_CONFIG_DIR="$HOME/.config/clai-talos"

detect_service_manager() {
  if [[ "$(uname)" == "Darwin" ]]; then
    echo "launchd"
  elif command -v systemctl &>/dev/null && [[ -d /run/systemd/system ]]; then
    echo "systemd"
  else
    echo "process"
  fi
}

install_systemd_service() {
  local unit="/etc/systemd/system/${SERVICE_NAME}.service"
  info "Installing systemd service..."
  sudo tee "$unit" >/dev/null <<EOF
[Unit]
Description=Clai TALOS Personal AI Assistant
Documentation=https://github.com/VynavinV/Clai_TALOS
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User=${USER}
Group=$(id -gn)
WorkingDirectory=${SCRIPT_DIR}/src
Environment=PYTHONUNBUFFERED=1
ExecStart=${SCRIPT_DIR}/src/venv/bin/python ${SCRIPT_DIR}/src/telegram_bot.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
  sudo systemctl restart "$SERVICE_NAME"
  sleep 3
  if systemctl is-active --quiet "$SERVICE_NAME"; then
    ok "Service installed and running (starts automatically at boot)"
    return 0
  fi
  fail "Service installed but did not start."
  sudo journalctl -u "$SERVICE_NAME" -n 15 --no-pager 2>/dev/null || true
  return 1
}

install_launchd_service() {
  local plist="$HOME/Library/LaunchAgents/${SERVICE_NAME}.plist"
  info "Installing launchd agent..."
  mkdir -p "$HOME/Library/LaunchAgents" "${SCRIPT_DIR}/src/logs"
  cat >"$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${SERVICE_NAME}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${SCRIPT_DIR}/src/venv/bin/python</string>
    <string>${SCRIPT_DIR}/src/telegram_bot.py</string>
  </array>
  <key>WorkingDirectory</key><string>${SCRIPT_DIR}/src</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${SCRIPT_DIR}/src/logs/talos.log</string>
  <key>StandardErrorPath</key><string>${SCRIPT_DIR}/src/logs/talos.log</string>
  <key>EnvironmentVariables</key>
  <dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
</dict>
</plist>
EOF
  launchctl unload "$plist" 2>/dev/null || true
  launchctl load -w "$plist" 2>/dev/null && { ok "Agent installed (starts at login)"; return 0; }
  fail "Could not load the launchd agent."
  return 1
}

install_process_service() {
  warn "No service manager found — starting as a plain background process."
  warn "It will keep running after you log out, but not survive a reboot."
  mkdir -p "${SCRIPT_DIR}/src/logs"
  ( cd "${SCRIPT_DIR}/src" && nohup venv/bin/python telegram_bot.py \
      >>"${SCRIPT_DIR}/src/logs/talos.log" 2>&1 & echo $! >"${SCRIPT_DIR}/src/logs/talos.pid" )
  sleep 2
  local pid
  pid=$(cat "${SCRIPT_DIR}/src/logs/talos.pid" 2>/dev/null || echo "")
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    ok "Running in the background (pid ${pid})"
    return 0
  fi
  fail "Background process did not stay up."
  return 1
}

install_clai_command() {
  local target="" src="${SCRIPT_DIR}/scripts/clai.sh"
  [[ -f "$src" ]] || { warn "scripts/clai.sh is missing; skipping the clai command."; return 1; }

  # Prefer a system-wide location so the command works from any shell.
  if sudo -n true 2>/dev/null && [[ -d /usr/local/bin ]]; then
    target="/usr/local/bin/clai"
    sudo cp "$src" "$target" && sudo chmod 755 "$target"
  else
    mkdir -p "$HOME/.local/bin"
    target="$HOME/.local/bin/clai"
    cp "$src" "$target" && chmod 755 "$target"
    case ":$PATH:" in
      *":$HOME/.local/bin:"*) ;;
      *)
        for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
          [[ -f "$rc" ]] || continue
          grep -q 'HOME/.local/bin' "$rc" 2>/dev/null && continue
          echo 'export PATH="$HOME/.local/bin:$PATH"' >>"$rc"
        done
        warn "Added ~/.local/bin to your PATH — open a new shell, or run: export PATH=\"\$HOME/.local/bin:\$PATH\""
        ;;
    esac
  fi

  mkdir -p "$CLAI_CONFIG_DIR"
  cat >"$CLAI_CONFIG_DIR/env" <<EOF
# Written by start.sh — tells the clai command where TALOS lives.
INSTALL_DIR="${SCRIPT_DIR}"
SERVICE_MANAGER="${SERVICE_MANAGER_KIND}"
SERVICE_NAME="${SERVICE_NAME}"
WEB_PORT="${WEB_PORT}"
EOF
  ok "Installed the 'clai' command"
  return 0
}

setup_background_service() {
  SERVICE_MANAGER_KIND="$(detect_service_manager)"

  # Nothing must hold the port when the service takes over.
  if [[ "$SERVICE_MANAGER_KIND" == "systemd" ]]; then
    sudo systemctl stop "$SERVICE_NAME" 2>/dev/null || true
  fi

  local installed=0
  case "$SERVICE_MANAGER_KIND" in
    systemd) install_systemd_service && installed=1 ;;
    launchd) install_launchd_service && installed=1 ;;
    *)       install_process_service && installed=1 ;;
  esac

  install_clai_command || true
  return $(( installed == 1 ? 0 : 1 ))
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

  ensure_tailscaled_running || true
  if tailscale status &>/dev/null; then
    ok "Tailscale is connected"
  else
    # Runs `tailscale up` for the user and prints the login link here, rather
    # than asking them to open a second terminal.
    ensure_tailscale_up || exit 1
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
  # One shared path for the whole Tailscale bring-up, so this mode and the
  # normal start behave identically instead of drifting apart.
  setup_tailscale_full funnel || {
    warn "Continuing without HTTPS — fix it later with 'clai repair'."
  }

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

  # Full Tailscale bring-up: install, daemon, login, operator, HTTPS.
  setup_tailscale_full funnel || true

  echo ""
  echo -e "${BOLD}  Run in the background?${RESET}"
  echo -e "${DIM}  Yes: installs a service so it keeps running after you close SSH,${RESET}"
  echo -e "${DIM}       restarts if it crashes, and comes back after a reboot.${RESET}"
  echo -e "${DIM}  No:  runs here in this terminal and stops when you disconnect.${RESET}"
  echo ""
  prompt RUN_BG "Run in background? [Y/n]" "Y"

  case "$RUN_BG" in
    [Yy]*)
      echo ""
      if setup_background_service; then
        URL=$(get_tailscale_url)
        echo ""
        echo -e "${GREEN}${BOLD}  ─────────────────────────────────────${RESET}"
        echo -e "${GREEN}${BOLD}  Clai TALOS is running in the background${RESET}"
        echo -e "${GREEN}${BOLD}  ─────────────────────────────────────${RESET}"
        echo ""
        echo -e "  Dashboard:  ${BOLD}${URL}${RESET}"
        echo ""
        echo -e "  ${BOLD}clai status${RESET}   ${DIM}is it healthy, and where do I reach it${RESET}"
        echo -e "  ${BOLD}clai logs -f${RESET}  ${DIM}watch what it is doing${RESET}"
        echo -e "  ${BOLD}clai check${RESET}    ${DIM}test every tool (no AI model involved)${RESET}"
        echo -e "  ${BOLD}clai repair${RESET}   ${DIM}auto-fix anything broken${RESET}"
        echo -e "  ${BOLD}clai restart${RESET}  ${DIM}restart it${RESET}"
        echo ""
        info "You can safely close this SSH session now."
        echo ""
        exit 0
      fi
      warn "Background setup failed — starting in the foreground instead."
      ;;
  esac

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
