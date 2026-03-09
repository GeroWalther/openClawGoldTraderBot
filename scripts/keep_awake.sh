#!/bin/bash
# Keep Mac awake during trading hours (07-21 UTC = 08-22 CET).
# Runs caffeinate to prevent sleep, only during market hours.
# Install: launchctl load ~/Library/LaunchAgents/com.gold-trader.keepawake.plist

HOUR=$(date -u +%H | sed 's/^0//')
DAY=$(date -u +%u)  # 1=Mon, 7=Sun

# Only Mon-Fri, 07-21 UTC
if [ "$DAY" -ge 1 ] && [ "$DAY" -le 5 ] && [ "$HOUR" -ge 7 ] && [ "$HOUR" -lt 21 ]; then
    # -d = prevent display sleep, -i = prevent idle sleep, -s = prevent system sleep
    # Run for 5 minutes (this script is called every 5 min by launchd)
    /usr/bin/caffeinate -dis -t 330 &
fi
