#!/bin/bash
set -e

# Write .env.production for cron scripts (they read from this path)
cp /app/.env /app/.env.production 2>/dev/null || true

# Install crontab for M5 scalp scanner
cat > /etc/cron.d/gold-trader << 'CRON'
PATH=/usr/local/bin:/usr/bin:/bin
SHELL=/bin/bash

# M5 Scalp — every 5 min, 07-21 UTC, Mon-Fri
2-59/5 7-21 * * 1-5  root  /app/cron/scan_scalp.sh >> /app/journal/cron.log 2>&1

# M15 Sensei (BTC) — every 15 min, 24/7 (BTC trades all week)
*/15 * * * *  root  /app/cron/scan_sensei.sh >> /app/journal/cron.log 2>&1

# M15 BB Bounce (AUDUSD) — every 15 min, 07-21 UTC, Mon-Fri
4-59/15 7-21 * * 1-5  root  /app/cron/scan_bb_bounce.sh >> /app/journal/cron.log 2>&1

# NY ORB (NZDUSD) — every 5 min, 13-16 UTC, Mon-Fri (NY open window)
3-59/5 13-16 * * 1-5  root  /app/cron/scan_ny_orb.sh >> /app/journal/cron.log 2>&1

# Trade monitor — every 5min during market hours, Mon-Fri
*/5 7-21 * * 1-5  root  /app/cron/monitor.sh >> /app/journal/cron.log 2>&1

# EOD close — 20:55 UTC, close all positions before swap cutoff
55 20 * * 1-5  root  /app/cron/eod_close.sh >> /app/journal/cron.log 2>&1

# Daily summary — 21:00 UTC
0 21 * * 1-5  root  /app/cron/daily_summary.sh >> /app/journal/cron.log 2>&1

CRON
chmod 0644 /etc/cron.d/gold-trader

# Start cron daemon in background
cron

echo "Starting gold-trader (AUDUSD M5 Scalp on IC Markets)..."
exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8001
