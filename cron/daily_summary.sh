#!/bin/bash
# Daily summary — runs at 21:00 UTC (22:00 Berlin).
# Sends end-of-day recap to Telegram.

source "$(dirname "$0")/common.sh"

DATE=$(date -u '+%Y-%m-%d')
CSV_FILE="$JOURNAL_DIR/summaries/summaries.csv"

ensure_csv_header "$CSV_FILE" \
    "date,account_balance,daily_pnl,trades_closed,wins,losses,open_positions,unrealized_pnl,intraday_signals,swing_signals,scalp_signals"

log "DAILY SUMMARY starting for $DATE"

# Fetch data from three endpoints
status_json=$(api_get "/api/v1/positions/status")
analytics_json=$(api_get "/api/v1/analytics?from_date=$DATE")
journal_json=$(api_get "/api/v1/journal?from_date=$DATE")

# Parse positions/account data
read -r account_balance open_count unrealized_pnl pending_count < <(
    echo "$status_json" | python3 -c "
import sys, json
d = json.load(sys.stdin)
acct = d.get('account', {})
balance = acct.get('NetLiquidation', acct.get('net_liquidation', acct.get('balance', 'N/A')))
positions = d.get('positions', [])
pending = d.get('pending_orders', [])
total_upnl = sum(float(p.get('unrealized_pnl', p.get('pnl', 0)) or 0) for p in positions)
print(f'{balance} {len(positions)} {total_upnl:.2f} {len(pending)}')
" 2>/dev/null || echo "N/A 0 0.00 0"
)

# Parse analytics
read -r daily_pnl trades_closed wins losses win_rate < <(
    echo "$analytics_json" | python3 -c "
import sys, json
d = json.load(sys.stdin)
pnl = d.get('total_pnl', 0) or 0
total = d.get('total_trades', 0) or 0
w = d.get('winning_trades', 0) or 0
l = d.get('losing_trades', 0) or 0
wr = d.get('win_rate', 0) or 0
print(f'{pnl:.2f} {total} {w} {l} {wr:.1f}')
" 2>/dev/null || echo "0.00 0 0 0 0.0"
)

# Parse journal — count intraday vs swing vs scalp signals
read -r intraday_signals swing_signals scalp_signals total_analyses < <(
    echo "$journal_json" | python3 -c "
import sys, json
entries = json.load(sys.stdin)
if not isinstance(entries, list):
    entries = []
intraday = sum(1 for e in entries if e.get('source', '') == 'cron_intraday')
swing = sum(1 for e in entries if e.get('source', '') == 'cron_swing')
scalp = sum(1 for e in entries if e.get('source', '') == 'cron_m5_scalp')
print(f'{intraday} {swing} {scalp} {len(entries)}')
" 2>/dev/null || echo "0 0 0 0"
)

# Count today's scans from CSV files
intraday_scans=$(grep -c "^${DATE}" "$JOURNAL_DIR/intraday/scans.csv" 2>/dev/null || echo "0")
swing_scans=$(grep -c "^${DATE}" "$JOURNAL_DIR/swing/scans.csv" 2>/dev/null || echo "0")
scalp_scans=$(grep -c "^${DATE}" "$JOURNAL_DIR/scalp/scans.csv" 2>/dev/null || echo "0")

# Build summary JSON
summary_json=$(python3 -c "
import json
d = {
    'date': '$DATE',
    'account_balance': '$account_balance',
    'daily_pnl': float('$daily_pnl'),
    'trades_closed': int('$trades_closed'),
    'wins': int('$wins'),
    'losses': int('$losses'),
    'win_rate': float('$win_rate'),
    'open_positions': int('$open_count'),
    'unrealized_pnl': float('$unrealized_pnl'),
    'pending_orders': int('$pending_count'),
    'intraday_signals': int('$intraday_signals'),
    'swing_signals': int('$swing_signals'),
    'scalp_signals': int('$scalp_signals'),
    'total_analyses': int('$total_analyses'),
    'intraday_scans': int('$intraday_scans'),
    'swing_scans': int('$swing_scans'),
    'scalp_scans': int('$scalp_scans'),
}
print(json.dumps(d, indent=2))
" 2>/dev/null)

# Save summary
echo "$summary_json" > "$JOURNAL_DIR/summaries/${DATE}.json"
echo "$summary_json" > "$JOURNAL_DIR/latest_summary.json"

# Append CSV row
echo "${DATE},${account_balance},${daily_pnl},${trades_closed},${wins},${losses},${open_count},${unrealized_pnl},${intraday_signals},${swing_signals},${scalp_signals}" >> "$CSV_FILE"

# Build Telegram message
win_loss_line=""
if [ "$trades_closed" != "0" ]; then
    win_loss_line="Wins: ${wins} | Losses: ${losses} | WR: ${win_rate}%"
fi

open_line=""
if [ "$open_count" != "0" ]; then
    open_line="
Open Positions: ${open_count} (uPnL: ${unrealized_pnl})"
fi

pending_line=""
if [ "$pending_count" != "0" ]; then
    pending_line="
Pending Orders: ${pending_count}"
fi

msg="*DAILY SUMMARY* -- ${DATE}

*Account*: ${account_balance}
*Today PnL*: ${daily_pnl}
*Trades Closed*: ${trades_closed}
${win_loss_line}${open_line}${pending_line}

*Scans Today*
Intraday: ${intraday_scans} scans, ${intraday_signals} signals
Swing: ${swing_scans} scans, ${swing_signals} signals
M5 Scalp: ${scalp_scans} scans, ${scalp_signals} signals
Total Analyses: ${total_analyses}"

log "DAILY SUMMARY: balance=$account_balance pnl=$daily_pnl trades=$trades_closed"

send_telegram "$msg"

log "DAILY SUMMARY done"
