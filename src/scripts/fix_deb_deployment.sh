#!/usr/bin/env bash
# fix_deb_deployment.sh - Fix common issues with .deb installation
# Usage: sudo bash fix_deb_deployment.sh

set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
LOG_PREFIX="[$SCRIPT_NAME]"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
  echo -e "${GREEN}${LOG_PREFIX}${NC} $*"
}

log_warn() {
  echo -e "${YELLOW}${LOG_PREFIX}${NC} $*"
}

log_error() {
  echo -e "${RED}${LOG_PREFIX}${NC} $*"
}

# Check if running as root or with sudo
if [[ $EUID -ne 0 ]]; then
  log_error "This script must be run as root (use: sudo bash $SCRIPT_NAME)"
  exit 1
fi

APP_DIR="/opt/clai-talos"
DATA_DIR="/var/lib/clai-talos"
USER_NAME="clai-talos"
ENV_FILE="$DATA_DIR/.env"

log_info "Starting Clai TALOS .deb deployment fixes..."

# ============================================================================
# Fix 1: Copy web template files to root level
# ============================================================================
log_info "Fixing web template file deployment..."

if [[ ! -d "$APP_DIR/src/web" ]]; then
  log_error "Source web directory not found: $APP_DIR/src/web"
  exit 1
fi

mkdir -p "$APP_DIR/web"
if cp "$APP_DIR/src/web"/*.html "$APP_DIR/web/" 2>/dev/null; then
  log_info "Web templates copied to $APP_DIR/web/"
else
  log_error "Failed to copy web templates"
  exit 1
fi

# Ensure proper ownership
chown -R "$USER_NAME:$USER_NAME" "$APP_DIR/web"
chmod 0644 "$APP_DIR/web"/*.html

log_info "Web templates fixed ✓"

# ============================================================================
# Fix 2: Ensure ca-certificates is installed
# ============================================================================
log_info "Checking ca-certificates package..."

if ! dpkg -l | grep -q "^ii.*ca-certificates"; then
  log_warn "ca-certificates not installed, installing now..."
  apt-get update >/dev/null 2>&1 || true
  apt-get install -y ca-certificates >/dev/null 2>&1
  log_info "ca-certificates installed ✓"
else
  log_info "ca-certificates already installed ✓"
fi

# ============================================================================
# Fix 3: Set SSL_CERT_FILE environment variable
# ============================================================================
log_info "Configuring SSL certificate path..."

if [[ ! -f "$ENV_FILE" ]]; then
  log_warn "Environment file not found: $ENV_FILE"
  mkdir -p "$DATA_DIR"
  touch "$ENV_FILE"
  chown "$USER_NAME:$USER_NAME" "$ENV_FILE"
  chmod 0600 "$ENV_FILE"
fi

# Check if SSL_CERT_FILE is already set
if grep -q "^SSL_CERT_FILE=" "$ENV_FILE"; then
  log_info "SSL_CERT_FILE already configured in $ENV_FILE"
else
  log_info "Adding SSL_CERT_FILE to environment..."
  echo "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt" >> "$ENV_FILE"
  log_info "SSL_CERT_FILE configured ✓"
fi

# ============================================================================
# Fix 4: Restart the service
# ============================================================================
log_info "Restarting clai-talos service..."

if systemctl is-active --quiet clai-talos; then
  systemctl stop clai-talos
  log_info "Service stopped"
fi

sleep 2

if systemctl start clai-talos; then
  log_info "Service started successfully ✓"
else
  log_error "Failed to start service"
  systemctl status clai-talos || true
  exit 1
fi

sleep 3

# ============================================================================
# Verification
# ============================================================================
log_info "Verifying fixes..."

# Check if service is running
if systemctl is-active --quiet clai-talos; then
  log_info "Service is running ✓"
else
  log_error "Service is not running"
  journalctl -u clai-talos -n 20 --no-pager
  exit 1
fi

# Check for web files
if [[ -f "$APP_DIR/web/activity.html" ]]; then
  log_info "Web templates present ✓"
else
  log_error "Web templates not found"
  exit 1
fi

# Check for SSL cert file
if [[ -f "/etc/ssl/certs/ca-certificates.crt" ]]; then
  log_info "SSL certificate file present ✓"
else
  log_warn "SSL certificate file not found - may cause HTTPS failures"
fi

# Check recent logs for errors
log_info "Checking recent logs for errors..."
if journalctl -u clai-talos -n 50 --no-pager | grep -i "error\|failed\|exception" | head -5; then
  log_warn "Some errors found in logs - review with: sudo journalctl -u clai-talos -n 100 --no-pager"
else
  log_info "No obvious errors in recent logs ✓"
fi

log_info ""
log_info "═══════════════════════════════════════════════════════════════"
log_info "All fixes applied successfully!"
log_info "═══════════════════════════════════════════════════════════════"
log_info ""
log_info "Next steps:"
log_info "  1. Test the dashboard: http://<server-ip>:8080"
log_info "  2. Check OTA status in Settings > Updates"
log_info "  3. Review logs: sudo journalctl -u clai-talos -f"
log_info ""

exit 0
