#!/bin/bash

echo "Starting Clai TALOS..."
echo ""

# Run setup
python3 setup.py
if [ $? -ne 0 ]; then
    echo "Setup failed. Please fix errors and try again."
    exit 1
fi

echo ""
echo "Starting bot..."
echo ""

# Start bot
python3 telegram_bot.py
