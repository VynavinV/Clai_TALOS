#!/bin/bash

echo "Starting Clai TALOS..."
echo ""

# Ensure projects directory exists
mkdir -p "$(dirname "$0")/projects"

# Run setup
python3 setup.py
if [ $? -ne 0 ]; then
    echo "Setup failed. Please fix errors and try again."
    exit 1
fi

# Setup Tailscale Funnel
WEB_PORT="${WEB_PORT:-8080}"
if command -v tailscale &>/dev/null; then
    # Check if tailscale is connected
    if tailscale status &>/dev/null; then
        # Check if funnel is already proxying our port
        if ! tailscale funnel status 2>/dev/null | grep -q "$WEB_PORT"; then
            echo "[tailscale] Setting up Funnel on port $WEB_PORT..."
            tailscale funnel --bg "$WEB_PORT" 2>/dev/null
            if [ $? -eq 0 ]; then
                echo "[tailscale] Funnel active"
            else
                echo "[tailscale] Funnel setup failed (may need: tailscale up --operator=$USER)"
            fi
        fi
    else
        echo "[tailscale] Not connected. Run: tailscale up"
    fi
else
    echo "[tailscale] Not installed. Install from https://tailscale.com/download"
fi

echo ""
echo "Starting bot..."
echo ""

# Start bot using venv python
./venv/bin/python telegram_bot.py
