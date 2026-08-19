#!/bin/bash
# clai — control and inspect a background Clai TALOS install.
#
# Installed to a directory on PATH by start.sh. Reads its configuration from
# ~/.config/clai-talos/env, which records where TALOS lives and which service
# manager is in charge (systemd, launchd, or a plain background process).

set -u

CONFIG_FILE="${CLAI_CONFIG:-$HOME/.config/clai-talos/env}"

BOLD="\033[1m"; DIM="\033[2m"; RESET="\033[0m"
GREEN="\033[32m"; RED="\033[31m"; YELLOW="\033[33m"; CYAN="\033[36m"

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo -e "${RED}clai is not configured.${RESET}" >&2
  echo "Expected $CONFIG_FILE — re-run ./start.sh --headless to set it up." >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$CONFIG_FILE"

INSTALL_DIR="${INSTALL_DIR:-}"
SERVICE_MANAGER="${SERVICE_MANAGER:-none}"
SERVICE_NAME="${SERVICE_NAME:-clai-talos}"
WEB_PORT="${WEB_PORT:-8080}"
SRC_DIR="$INSTALL_DIR/src"
PID_FILE="$SRC_DIR/logs/talos.pid"
LOG_FILE="$SRC_DIR/logs/talos.log"
PY="$SRC_DIR/venv/bin/python"

ok()   { echo -e "${GREEN}✓${RESET} $1"; }
bad()  { echo -e "${RED}✗${RESET} $1"; }
warn() { echo -e "${YELLOW}!${RESET} $1"; }

need_install_dir() {
  if [[ -z "$INSTALL_DIR" || ! -d "$SRC_DIR" ]]; then
    bad "TALOS install not found at '${INSTALL_DIR:-<unset>}'."
    exit 1
  fi
}

# ── Service control, abstracted over the three managers ────────────────

svc_running() {
  case "$SERVICE_MANAGER" in
    systemd) systemctl is-active --quiet "$SERVICE_NAME" ;;
    launchd) launchctl list 2>/dev/null | grep -q "$SERVICE_NAME" ;;
    process) [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null ;;
    *) return 1 ;;
  esac
}

svc_enabled_at_boot() {
  case "$SERVICE_MANAGER" in
    systemd) systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null ;;
    launchd) [[ -f "$HOME/Library/LaunchAgents/$SERVICE_NAME.plist" ]] ;;
    *) return 1 ;;
  esac
}

svc_start() {
  case "$SERVICE_MANAGER" in
    systemd) sudo systemctl start "$SERVICE_NAME" ;;
    launchd) launchctl load -w "$HOME/Library/LaunchAgents/$SERVICE_NAME.plist" 2>/dev/null ;;
    process)
      mkdir -p "$SRC_DIR/logs"
      ( cd "$SRC_DIR" && nohup "$PY" telegram_bot.py >>"$LOG_FILE" 2>&1 & echo $! >"$PID_FILE" )
      ;;
    *) bad "No service manager configured."; return 1 ;;
  esac
}

svc_stop() {
  case "$SERVICE_MANAGER" in
    systemd) sudo systemctl stop "$SERVICE_NAME" ;;
    launchd) launchctl unload "$HOME/Library/LaunchAgents/$SERVICE_NAME.plist" 2>/dev/null ;;
    process)
      if [[ -f "$PID_FILE" ]]; then
        kill "$(cat "$PID_FILE")" 2>/dev/null || true
        rm -f "$PID_FILE"
      fi
      ;;
    *) bad "No service manager configured."; return 1 ;;
  esac
}

dashboard_url() {
  local dns
  if command -v tailscale &>/dev/null && tailscale status &>/dev/null; then
    dns=$(tailscale status --json 2>/dev/null \
          | "$PY" -c "import sys,json;print(json.load(sys.stdin).get('Self',{}).get('DNSName','').rstrip('.'))" 2>/dev/null || true)
    if [[ -n "$dns" ]]; then
      if tailscale serve status 2>/dev/null | grep -q "$WEB_PORT"; then
        echo "https://${dns}"; return
      fi
      echo "http://${dns}:${WEB_PORT}"; return
    fi
  fi
  echo "http://localhost:${WEB_PORT}"
}

# ── Commands ───────────────────────────────────────────────────────────

cmd_status() {
  need_install_dir
  echo ""
  echo -e "${CYAN}${BOLD}  Clai TALOS${RESET}"
  echo ""

  if svc_running; then
    ok "Service is running (${SERVICE_MANAGER})"
  else
    bad "Service is not running (${SERVICE_MANAGER})"
  fi

  if svc_enabled_at_boot; then
    ok "Starts automatically at boot"
  elif [[ "$SERVICE_MANAGER" == "process" ]]; then
    warn "Background process only — will not survive a reboot"
  else
    warn "Not enabled at boot"
  fi

  # Is the dashboard actually answering?
  if command -v curl &>/dev/null; then
    if curl -sf -m 5 -o /dev/null "http://127.0.0.1:${WEB_PORT}/"; then
      ok "Dashboard responding on port ${WEB_PORT}"
    else
      bad "Dashboard not responding on port ${WEB_PORT}"
    fi
  fi

  if command -v tailscale &>/dev/null; then
    if tailscale status &>/dev/null; then
      if tailscale serve status 2>/dev/null | grep -q "$WEB_PORT"; then
        if tailscale funnel status 2>/dev/null | grep -q "$WEB_PORT"; then
          ok "HTTPS active (Funnel — public)"
        else
          ok "HTTPS active (Serve — tailnet only)"
        fi
      else
        bad "HTTPS not configured — run: clai repair"
      fi
    else
      bad "Tailscale not connected"
    fi
  fi

  echo ""
  echo -e "  ${BOLD}$(dashboard_url)${RESET}"
  echo ""
  echo -e "${DIM}  clai logs     follow output      clai check    test everything${RESET}"
  echo -e "${DIM}  clai restart  restart service    clai repair   auto-fix problems${RESET}"
  echo ""
}

cmd_logs() {
  need_install_dir
  local follow=false lines=100
  for a in "$@"; do
    case "$a" in
      -f|--follow) follow=true ;;
      -n) shift ;;
      [0-9]*) lines="$a" ;;
    esac
  done

  if [[ "$SERVICE_MANAGER" == "systemd" ]]; then
    if [[ "$follow" == true ]]; then
      journalctl -u "$SERVICE_NAME" -n "$lines" -f --no-hostname
    else
      journalctl -u "$SERVICE_NAME" -n "$lines" --no-pager --no-hostname
    fi
    return
  fi

  if [[ ! -f "$LOG_FILE" ]]; then
    bad "No log file at $LOG_FILE"
    exit 1
  fi
  if [[ "$follow" == true ]]; then
    tail -n "$lines" -f "$LOG_FILE"
  else
    tail -n "$lines" "$LOG_FILE"
  fi
}

cmd_start() {
  need_install_dir
  if svc_running; then ok "Already running."; return; fi
  svc_start && sleep 2
  svc_running && ok "Started." || { bad "Failed to start. Try: clai logs"; exit 1; }
}

cmd_stop() {
  need_install_dir
  svc_stop && ok "Stopped."
}

cmd_restart() {
  need_install_dir
  if [[ "$SERVICE_MANAGER" == "systemd" ]]; then
    sudo systemctl restart "$SERVICE_NAME"
  else
    svc_stop; sleep 1; svc_start
  fi
  sleep 2
  svc_running && ok "Restarted." || { bad "Did not come back up. Try: clai logs"; exit 1; }
}

cmd_check() {
  need_install_dir
  # The diagnostics suite is deterministic and never calls the model, so it is
  # safe to run from a shell command even when the assistant itself is broken.
  ( cd "$SRC_DIR" && "$PY" -c "
import asyncio, diagnostics
r = asyncio.run(diagnostics.run_all())
icons = {'ok':'\033[32m✓\033[0m','warn':'\033[33m!\033[0m','fail':'\033[31m✗\033[0m','skip':'\033[2m-\033[0m'}
print()
for c in r['checks']:
    print(f\"  {icons.get(c['status'],'?')} {c['name']:26} {c['detail'][:70]}\")
    if c['status'] in ('fail','warn') and (c['fix'] or c['reset']):
        print(f\"      \033[2m→ fixable: clai repair\033[0m\")
counts = r['counts']
print()
print(f\"  {counts['ok']} passed, {counts['warn']} warnings, {counts['fail']} failed, {counts['skip']} skipped\")
print()
raise SystemExit(1 if counts['fail'] else 0)
")
}

cmd_repair() {
  need_install_dir
  local confirm=""
  [[ "${1:-}" == "--all" ]] && confirm="True"
  ( cd "$SRC_DIR" && "$PY" -c "
import asyncio, diagnostics
confirm = ${confirm:-False}
r = asyncio.run(diagnostics.run_auto_repair(confirm_destructive=confirm))
for a in r['applied']:
    print(('  \033[32m✓\033[0m ' if a['ok'] else '  \033[31m✗\033[0m ') + a['repair'] + ': ' + a.get('message',''))
for s in r['skipped']:
    print('  \033[2m-\033[0m ' + s['repair'] + ' (' + s['reason'] + ')')
if not r['applied'] and not r['skipped']:
    print('  Nothing to repair.')
c = r['after']['counts']
print()
print(f\"  Now: {c['ok']} passed, {c['warn']} warnings, {c['fail']} failed\")
")
}

cmd_url() { need_install_dir; dashboard_url; }

cmd_enable() {
  case "$SERVICE_MANAGER" in
    systemd) sudo systemctl enable "$SERVICE_NAME" && ok "Will start at boot." ;;
    launchd) launchctl load -w "$HOME/Library/LaunchAgents/$SERVICE_NAME.plist" && ok "Will start at login." ;;
    *) bad "Boot autostart needs systemd or launchd. Re-run ./start.sh --headless." ;;
  esac
}

cmd_disable() {
  case "$SERVICE_MANAGER" in
    systemd) sudo systemctl disable "$SERVICE_NAME" && ok "Will not start at boot." ;;
    launchd) launchctl unload -w "$HOME/Library/LaunchAgents/$SERVICE_NAME.plist" && ok "Disabled." ;;
    *) bad "Nothing to disable." ;;
  esac
}

cmd_help() {
  cat <<EOF

  clai — control your Clai TALOS assistant

    clai status          service, dashboard and HTTPS state
    clai logs [-f] [N]   show output (-f to follow)
    clai start           start it
    clai stop            stop it
    clai restart         restart it
    clai check           run every self-test (no AI model involved)
    clai repair          auto-fix safe problems (--all includes destructive)
    clai url             print the dashboard URL
    clai enable          start automatically at boot
    clai disable         stop starting at boot

EOF
}

case "${1:-status}" in
  status)  shift; cmd_status "$@" ;;
  logs)    shift; cmd_logs "$@" ;;
  start)   shift; cmd_start "$@" ;;
  stop)    shift; cmd_stop "$@" ;;
  restart) shift; cmd_restart "$@" ;;
  check)   shift; cmd_check "$@" ;;
  repair)  shift; cmd_repair "$@" ;;
  url)     shift; cmd_url "$@" ;;
  enable)  shift; cmd_enable "$@" ;;
  disable) shift; cmd_disable "$@" ;;
  -h|--help|help) cmd_help ;;
  *) echo "Unknown command: $1"; cmd_help; exit 1 ;;
esac
