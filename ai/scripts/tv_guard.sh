#!/bin/bash
# tv_guard.sh — ensures TradingView Desktop is running with CDP port 9222.
# Runs every minute. If port dead, relaunches TV with correct flag.
# This protects against accidental quit / relaunch-without-flag.

if curl -sf --max-time 2 http://127.0.0.1:9222/json/version > /dev/null 2>&1; then
    exit 0
fi

# Port dead — check if TV is running at all
if pgrep -x "TradingView" > /dev/null 2>&1; then
    # Running but without flag — must quit first
    osascript -e 'tell application "TradingView" to quit' 2>/dev/null
    sleep 1
fi

open -na TradingView --args --remote-debugging-port=9222 2>/dev/null
LOGFILE="/Users/gerosmacbookpro/gold-trader/journal/ai/tv_guard.log"
echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] Relaunched TradingView with CDP port" >> "$LOGFILE"
