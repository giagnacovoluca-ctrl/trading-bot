#!/bin/bash
cd /home/ubuntu/GIT/FraDodo

# Kill any existing screens if we run this again
screen -X -S fradodo_bot quit 2>/dev/null
screen -X -S fradodo_tunnel quit 2>/dev/null

# Start the bot/API in a screen
screen -dmS fradodo_bot bash -c 'source venv/bin/activate && ./venv/bin/python main.py'

# Tunnel is now managed by the Watchdog directly.

# Start the watchdog in a screen
screen -X -S fradodo_watchdog quit 2>/dev/null
screen -dmS fradodo_watchdog bash -c 'source venv/bin/activate && python watchdog.py'

echo "FraDodo Bot, Dashboard and Watchdog started persistently in the background!"
echo "Use 'screen -r fradodo_bot' to view bot logs."
echo "Use 'screen -r fradodo_tunnel' to view localtunnel logs."
echo "Use 'screen -r fradodo_watchdog' to view watchdog logs."
