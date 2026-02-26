#!/bin/bash
# Install crontab entries on VPS.
# Run once after deployment: bash /opt/gold-trader/cron/setup_cron.sh

set -euo pipefail

CRON_DIR="/opt/gold-trader/cron"
LOG="/opt/gold-trader/journal/cron.log"

# Ensure journal dirs exist
mkdir -p /opt/gold-trader/journal/{intraday/scans,swing/scans,monitors,summaries}

# Build crontab (preserving any existing non-gold-trader entries)
EXISTING=$(crontab -l 2>/dev/null | grep -v '/opt/gold-trader/cron/' | grep -v '^#.*gold-trader' || true)

NEW_CRON=$(cat <<'CRONTAB'
# --- gold-trader automated monitoring ---

# Intraday scan — every hour 07-21 UTC, Mon-Fri (runs at :00)
0 7-21 * * 1-5  /opt/gold-trader/cron/scan_intraday.sh >> /opt/gold-trader/journal/cron.log 2>&1

# Swing scan — 08:05, 13:05, 19:05 UTC (staggered +5min to avoid race with intraday)
5 8,13,19 * * 1-5  /opt/gold-trader/cron/scan_swing.sh >> /opt/gold-trader/journal/cron.log 2>&1

# Trade monitor — every 30min during market hours (active risk management)
*/30 7-21 * * 1-5  /opt/gold-trader/cron/monitor.sh >> /opt/gold-trader/journal/cron.log 2>&1

# Daily summary — 21:00 UTC (= 22:00 Berlin CET)
0 21 * * 1-5  /opt/gold-trader/cron/daily_summary.sh >> /opt/gold-trader/journal/cron.log 2>&1

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
