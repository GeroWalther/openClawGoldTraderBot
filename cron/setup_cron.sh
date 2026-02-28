#!/bin/bash
# Install crontab entries on VPS.
# Run once after deployment: bash /opt/gold-trader/cron/setup_cron.sh

set -euo pipefail

CRON_DIR="/opt/gold-trader/cron"
LOG="/opt/gold-trader/journal/cron.log"

# Ensure journal dirs exist
mkdir -p /opt/gold-trader/journal/{intraday/scans,swing/scans,scalp/scans,daily/scans,monitors,summaries}

# Build crontab (preserving any existing non-gold-trader entries)
# Remove: marker block, cron commands, and orphaned comment lines from previous deploys
EXISTING=$(crontab -l 2>/dev/null \
    | sed '/^# --- gold-trader/,/^# --- end gold-trader/d' \
    | grep -v '/opt/gold-trader/cron/' \
    | grep -v '^# .*scan\|^# .*Scalp\|^# .*monitor\|^# .*summary\|^# .*strategy' \
    | sed '/^[[:space:]]*$/d' \
    || true)

NEW_CRON=$(cat <<'CRONTAB'
# --- gold-trader automated monitoring ---

# Intraday scan — every hour 07-21 UTC, every day (runs at :00)
0 7-21 * * *  /opt/gold-trader/cron/scan_intraday.sh >> /opt/gold-trader/journal/cron.log 2>&1

# Daily strategy scan — 08:10 UTC, every day (after swing scan at 08:05)
10 8 * * *  /opt/gold-trader/cron/scan_daily.sh >> /opt/gold-trader/journal/cron.log 2>&1

# Swing scan — 08:05, 13:05, 19:05 UTC, every day (staggered +5min to avoid race with intraday)
5 8,13,19 * * *  /opt/gold-trader/cron/scan_swing.sh >> /opt/gold-trader/journal/cron.log 2>&1

# M5 Scalp — every 5 min, 07-21 UTC, every day (matches backtest frequency)
2-59/5 7-21 * * *  /opt/gold-trader/cron/scan_scalp.sh >> /opt/gold-trader/journal/cron.log 2>&1

# Trade monitor — every 5min during market hours, every day (active risk management)
*/5 7-21 * * *  /opt/gold-trader/cron/monitor.sh >> /opt/gold-trader/journal/cron.log 2>&1

# Daily summary — 21:00 UTC, every day (= 22:00 Berlin CET)
0 21 * * *  /opt/gold-trader/cron/daily_summary.sh >> /opt/gold-trader/journal/cron.log 2>&1

# --- end gold-trader ---
CRONTAB
)

# Combine existing + new
if [ -n "$EXISTING" ]; then
    echo "${EXISTING}
${NEW_CRON}" | crontab -
else
    echo "$NEW_CRON" | crontab -
fi

echo "Crontab installed. Current entries:"
crontab -l
echo ""
echo "Journal directory:"
ls -la /opt/gold-trader/journal/
