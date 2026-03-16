#!/bin/bash
# Keep Mac awake while the gold-trader Docker container is running.
# Uses caffeinate -s -w <PID> to prevent system sleep for the container's lifetime.
# Only active during market hours (Mon-Fri 07:00-21:30 UTC).
#
# Install LaunchAgent:
#   cp scripts/com.gold-trader.keepawake.plist ~/Library/LaunchAgents/
#   launchctl load ~/Library/LaunchAgents/com.gold-trader.keepawake.plist

set -euo pipefail

HOUR=$(date -u +%H | sed 's/^0//')
MIN=$(date -u +%M | sed 's/^0//')
DAY=$(date -u +%u)  # 1=Mon, 7=Sun

# Only Mon-Fri, 07:00-21:30 UTC
if [ "$DAY" -lt 1 ] || [ "$DAY" -gt 5 ]; then
    exit 0
fi
if [ "$HOUR" -lt 7 ] || [ "$HOUR" -gt 21 ]; then
    exit 0
fi
if [ "$HOUR" -eq 21 ] && [ "$MIN" -ge 30 ]; then
    exit 0
fi

# Get the gold-trader container PID
CONTAINER_PID=$(docker inspect --format '{{.State.Pid}}' gold-trader 2>/dev/null || echo "0")

if [ "$CONTAINER_PID" = "0" ] || [ -z "$CONTAINER_PID" ]; then
    echo "$(date -u '+%Y-%m-%d %H:%M UTC') gold-trader container not running, skipping caffeinate"
    exit 0
fi

echo "$(date -u '+%Y-%m-%d %H:%M UTC') Keeping Mac awake for gold-trader (PID $CONTAINER_PID)"
# -s = prevent system sleep, -w = wait for process to exit
exec /usr/bin/caffeinate -i -s -w "$CONTAINER_PID"
