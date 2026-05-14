#!/bin/bash
set -e

# Write .env.production for cron scripts (they read from this path)
cp /app/.env /app/.env.production 2>/dev/null || true

# TRADING_MODE: "classic" (default) runs all fixed-strategy cron jobs in-container.
#               "ai"     runs ONLY monitor + eod + daily_summary here; AI scripts run on host.
TRADING_MODE="${TRADING_MODE:-classic}"

echo "Starting gold-trader in TRADING_MODE=$TRADING_MODE"

if [ "$TRADING_MODE" = "ai" ]; then
    # AI mode: container ONLY runs the daily summary + TradeCloseMonitor (built-in, polls broker every 30s).
    # ALL position management is done by host-side ai_manage.sh — no monitor.sh, no fixed eod_close.sh.
    # ai_manage.sh + ai_eod.sh on host handle trailing SL, partial close, weekend decisions per-position.
    cat > /etc/cron.d/gold-trader << 'CRON'
PATH=/usr/local/bin:/usr/bin:/bin
SHELL=/bin/bash

# Daily summary — 21:00 UTC (the only in-container cron job in AI mode)
0 21 * * 1-5  root  /app/cron/daily_summary.sh >> /app/journal/cron.log 2>&1
CRON
else
    # Classic mode: the full fixed-strategy cron pipeline
    cat > /etc/cron.d/gold-trader << 'CRON'
PATH=/usr/local/bin:/usr/bin:/bin
SHELL=/bin/bash

# M5 Scalp — every 5 min, 07-21 UTC, Mon-Fri
2-59/5 7-21 * * 1-5  root  /app/cron/scan_scalp.sh >> /app/journal/cron.log 2>&1

# M15 Sensei (BTC) — every 15 min, 24/7
*/15 * * * *  root  /app/cron/scan_sensei.sh >> /app/journal/cron.log 2>&1

# M15 BB Bounce (AUDUSD) — every 15 min, 07-21 UTC, Mon-Fri
4-59/15 7-21 * * 1-5  root  /app/cron/scan_bb_bounce.sh >> /app/journal/cron.log 2>&1

# NY ORB (NZDUSD) — every 5 min, 13:48-16:58 UTC, Mon-Fri
48,53,58 13 * * 1-5  root  /app/cron/scan_ny_orb.sh >> /app/journal/cron.log 2>&1
3-59/5 14-16 * * 1-5  root  /app/cron/scan_ny_orb.sh >> /app/journal/cron.log 2>&1

# Trade monitor — every 5min during market hours, Mon-Fri
*/5 7-21 * * 1-5  root  /app/cron/monitor.sh >> /app/journal/cron.log 2>&1

# EOD close — Friday only at 20:55 UTC
55 20 * * 5  root  /app/cron/eod_close.sh >> /app/journal/cron.log 2>&1

# Daily summary — 21:00 UTC
0 21 * * 1-5  root  /app/cron/daily_summary.sh >> /app/journal/cron.log 2>&1
CRON
fi

chmod 0644 /etc/cron.d/gold-trader

# Start cron daemon in background
cron

echo "Starting gold-trader FastAPI (mode=$TRADING_MODE)..."
exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8001
