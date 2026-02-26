#!/bin/bash
# VPS Trading Dashboard — run locally to get full status from VPS.
# Usage: bash vps-status.sh [command]
#   bash vps-status.sh           # full dashboard
#   bash vps-status.sh positions # positions only
#   bash vps-status.sh scans     # latest scan results
#   bash vps-status.sh journal   # journal + analytics
#   bash vps-status.sh logs      # cron logs
#   bash vps-status.sh csv       # CSV scan history

set -euo pipefail

VPS="root@77.37.125.65"
REMOTE_DIR="/opt/gold-trader"
JOURNAL="$REMOTE_DIR/journal"

# Get API key from VPS
api_key() {
    ssh "$VPS" "grep '^API_SECRET_KEY=' $REMOTE_DIR/.env.production | cut -d= -f2-"
}

# API call helper
api() {
    local endpoint="$1"
    ssh "$VPS" "curl -sf -H 'x-api-key: $(api_key)' 'http://localhost:8001${endpoint}'"
}

# --- Sections ---

show_positions() {
    echo "═══════════════════════════════════════════════════════════════"
    echo "  POSITIONS & ACCOUNT"
    echo "═══════════════════════════════════════════════════════════════"
    api "/api/v1/positions/status" | python3 -c "
import sys, json
d = json.load(sys.stdin)

# Account
acct = d.get('account', {})
nlv = acct.get('NetLiquidation', 0)
cash = acct.get('TotalCashValue', 0)
avail = acct.get('AvailableFunds', 0)
print(f'  Account: NLV=\${nlv:,.2f}  Cash=\${cash:,.2f}  Available=\${avail:,.2f}')
print()

# Open positions
positions = d.get('positions', [])
if positions:
    print(f'  Open Positions ({len(positions)}):')
    print(f'  {\"Instrument\":<12} {\"Dir\":<5} {\"Size\":>6} {\"Entry\":>10} {\"Current\":>10} {\"PnL\":>10} {\"SL\":>10} {\"TP\":>10}')
    print(f'  {\"─\"*12} {\"─\"*5} {\"─\"*6} {\"─\"*10} {\"─\"*10} {\"─\"*10} {\"─\"*10} {\"─\"*10}')
    for p in positions:
        inst = p.get('instrument', p.get('contract', ''))
        direction = p.get('direction', p.get('side', ''))
        size = p.get('size', p.get('totalSize', 0))
        entry = p.get('entry_price', p.get('avg_cost', 0))
        current = p.get('current_price', 0)
        pnl = p.get('unrealized_pnl', p.get('unrealizedPnL', 0))
        sl = p.get('stop_loss', '-')
        tp = p.get('take_profit', '-')
        pnl_str = f'\${pnl:>+.2f}' if isinstance(pnl, (int, float)) else str(pnl)
        sl_str = f'{sl:.2f}' if isinstance(sl, (int, float)) else str(sl)
        tp_str = f'{tp:.2f}' if isinstance(tp, (int, float)) else str(tp)
        print(f'  {inst:<12} {direction:<5} {size:>6} {entry:>10.2f} {current:>10.2f} {pnl_str:>10} {sl_str:>10} {tp_str:>10}')
else:
    print('  No open positions.')
print()

# Pending orders
pending = d.get('pending_orders', [])
if pending:
    print(f'  Pending Orders ({len(pending)}):')
    for o in pending:
        inst = o.get('instrument', o.get('contract', ''))
        direction = o.get('direction', o.get('side', ''))
        otype = o.get('order_type', o.get('orderType', '?'))
        entry = o.get('entry_price', o.get('lmtPrice', o.get('auxPrice', '?')))
        print(f'    {inst} {direction} {otype} @ {entry}')
else:
    print('  No pending orders.')
print()

# Recent trades
trades = d.get('recent_trades', [])
if trades:
    print(f'  Recent Trades (last {len(trades)}):')
    print(f'  {\"Date\":<20} {\"Instrument\":<10} {\"Dir\":<5} {\"Size\":>6} {\"Entry\":>10} {\"PnL\":>10} {\"Status\":<10}')
    print(f'  {\"─\"*20} {\"─\"*10} {\"─\"*5} {\"─\"*6} {\"─\"*10} {\"─\"*10} {\"─\"*10}')
    for t in trades:
        date = str(t.get('created_at', ''))[:19]
        inst = t.get('epic', t.get('instrument', ''))
        direction = t.get('direction', '')
        size = t.get('size', 0)
        entry = t.get('entry_price') or 0
        pnl = t.get('pnl') or 0
        size = size or 0
        status = t.get('status', '')
        entry_str = f'{entry:>10.2f}' if entry else '         -'
        print(f'  {date:<20} {inst:<10} {direction:<5} {size:>6} {entry_str} \${pnl:>+9.2f} {status:<10}')
else:
    print('  No recent trades.')
"
}

show_scans() {
    echo "═══════════════════════════════════════════════════════════════"
    echo "  LATEST SCAN RESULTS"
    echo "═══════════════════════════════════════════════════════════════"

    echo "  --- Swing (latest) ---"
    ssh "$VPS" "cat $JOURNAL/swing/latest_scan.json 2>/dev/null || echo '{}'" | python3 -c "
import sys, json
d = json.load(sys.stdin)
instruments = d.get('instruments', [])
if not instruments:
    # Single instrument format
    if d.get('instrument'):
        instruments = [d]
if instruments:
    print(f'  {\"Instrument\":<12} {\"Price\":>10} {\"Score\":>8} {\"Dir\":<6} {\"Conv\":<8} {\"D1 Trend\":<10} {\"RSI\":>6} {\"Signal\":<12}')
    print(f'  {\"─\"*12} {\"─\"*10} {\"─\"*8} {\"─\"*6} {\"─\"*8} {\"─\"*10} {\"─\"*6} {\"─\"*12}')
    for item in instruments:
        inst = item.get('instrument', '?')
        price = item.get('price', {}).get('current', 0)
        sc = item.get('scoring', {})
        score = sc.get('total_score', 0)
        max_sc = sc.get('max_score', 0)
        direction = sc.get('direction') or '-'
        conviction = sc.get('conviction') or '-'
        signal_type = sc.get('signal_type', '-')
        d1 = item.get('technicals', {}).get('d1', {})
        trend = d1.get('trend', '?')
        rsi = d1.get('rsi', '?')
        rsi_str = f'{rsi:.1f}' if isinstance(rsi, (int, float)) else str(rsi)
        print(f'  {inst:<12} {price:>10.2f} {score:>4.1f}/{max_sc:<3.0f} {direction:<6} {conviction:<8} {trend:<10} {rsi_str:>6} {signal_type:<12}')
else:
    print('  No swing scan data.')
" 2>/dev/null || echo "  (no swing scan data yet)"

    echo ""
    echo "  --- Intraday (latest) ---"
    ssh "$VPS" "cat $JOURNAL/intraday/latest_scan.json 2>/dev/null || echo '{}'" | python3 -c "
import sys, json
d = json.load(sys.stdin)
if d.get('scoring'):
    sc = d.get('scoring', {})
    print(f'  Instrument: {d.get(\"instrument\",\"?\")}')
    print(f'  Price: {d.get(\"price\",{}).get(\"current\",\"?\")}')
    print(f'  Score: {sc.get(\"total_score\",0)}/{sc.get(\"max_score\",0)}')
    print(f'  Direction: {sc.get(\"direction\") or \"-\"}  Conviction: {sc.get(\"conviction\") or \"-\"}')
    print(f'  Signal type: {sc.get(\"signal_type\",\"-\")}')
    h1 = d.get('technicals',{}).get('h1',{})
    print(f'  H1: trend={h1.get(\"trend\",\"?\")} RSI={h1.get(\"rsi\",\"?\")}')
else:
    print('  No intraday scan data.')
" 2>/dev/null || echo "  (no intraday scan data yet)"
}

show_journal() {
    echo "═══════════════════════════════════════════════════════════════"
    echo "  JOURNAL & ANALYTICS"
    echo "═══════════════════════════════════════════════════════════════"

    echo "  --- Analytics ---"
    api "/api/v1/analytics" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'  Total trades: {d.get(\"total_trades\",0)}  Wins: {d.get(\"winning_trades\",0)}  Losses: {d.get(\"losing_trades\",0)}')
print(f'  Win rate: {d.get(\"win_rate\",0):.1f}%  Expectancy: \${d.get(\"expectancy\",0):.2f}  PF: {d.get(\"profit_factor\",0):.2f}')
print(f'  Total PnL: \${d.get(\"total_pnl\",0):.2f}  Max DD: {d.get(\"max_drawdown\",0):.1f}%')
streak = d.get('current_streak', 0)
streak_type = 'wins' if streak > 0 else 'losses' if streak < 0 else 'flat'
print(f'  Current streak: {abs(streak)} {streak_type}')

per_inst = d.get('per_instrument', {})
if per_inst:
    print()
    print(f'  Per Instrument:')
    for inst, stats in per_inst.items():
        print(f'    {inst}: {stats.get(\"trades\",0)} trades, WR={stats.get(\"win_rate\",0):.0f}%, PnL=\${stats.get(\"total_pnl\",0):.2f}')
" 2>/dev/null || echo "  (no analytics data)"

    echo ""
    echo "  --- Recent Journal Entries ---"
    api "/api/v1/journal" | python3 -c "
import sys, json
entries = json.load(sys.stdin)
if not entries:
    print('  No journal entries.')
    sys.exit()
for e in entries[:10]:
    date = str(e.get('created_at',''))[:16]
    inst = e.get('instrument','?')
    direction = e.get('direction','?')
    conv = e.get('conviction','?')
    score = e.get('total_score',0)
    source = e.get('source','?')
    outcome = e.get('outcome','')
    print(f'  {date}  {inst:<10} {direction:<5} {conv:<6} score={score:>5.1f}  src={source:<14} outcome={outcome or \"-\"}')
" 2>/dev/null || echo "  (no journal entries)"

    echo ""
    echo "  --- Cooldown Status ---"
    api "/api/v1/analytics/cooldown" | python3 -c "
import sys, json
d = json.load(sys.stdin)
can = '✓ CAN TRADE' if d.get('can_trade') else '✗ BLOCKED'
print(f'  {can}')
if d.get('cooldown_active'):
    print(f'  Reason: {d.get(\"cooldown_reason\",\"?\")}  Remaining: {d.get(\"cooldown_remaining_minutes\",0):.0f}min')
print(f'  Consecutive losses: {d.get(\"consecutive_losses\",0)}  Daily trades: {d.get(\"daily_trades_count\",0)}/{d.get(\"daily_trades_limit\",5)}')
print(f'  Daily PnL: \${d.get(\"daily_pnl\",0):.2f}  Limit: \${d.get(\"daily_loss_limit\",0):.2f}')
" 2>/dev/null || echo "  (cooldown data unavailable)"
}

show_csv() {
    echo "═══════════════════════════════════════════════════════════════"
    echo "  CSV SCAN HISTORY"
    echo "═══════════════════════════════════════════════════════════════"

    echo "  --- Swing Scans (last 20 rows) ---"
    ssh "$VPS" "tail -20 $JOURNAL/swing/scans.csv 2>/dev/null" | column -t -s, 2>/dev/null || echo "  (no swing CSV yet)"

    echo ""
    echo "  --- Intraday Scans (last 20 rows) ---"
    ssh "$VPS" "tail -20 $JOURNAL/intraday/scans.csv 2>/dev/null" | column -t -s, 2>/dev/null || echo "  (no intraday CSV yet)"

    echo ""
    echo "  --- Monitor History (last 20 rows) ---"
    ssh "$VPS" "tail -20 $JOURNAL/monitors/monitors.csv 2>/dev/null" | column -t -s, 2>/dev/null || echo "  (no monitor CSV yet)"
}

show_logs() {
    echo "═══════════════════════════════════════════════════════════════"
    echo "  CRON LOGS (last 40 lines)"
    echo "═══════════════════════════════════════════════════════════════"
    ssh "$VPS" "tail -40 $JOURNAL/cron.log 2>/dev/null" || echo "  (no cron log yet)"

    echo ""
    echo "  --- Service Status ---"
    ssh "$VPS" "systemctl is-active gold-trader && echo '  Bot: RUNNING' || echo '  Bot: STOPPED'"
    ssh "$VPS" "docker inspect -f '{{.State.Status}}' ib-gateway 2>/dev/null | sed 's/^/  IB Gateway: /' || echo '  IB Gateway: unknown'"

    echo ""
    echo "  --- Bot Logs (last 10) ---"
    ssh "$VPS" "journalctl -u gold-trader --no-pager -n 10 2>/dev/null" || true
}

# --- Main ---

CMD="${1:-all}"

echo ""
echo "  GOLD TRADER VPS DASHBOARD — $(date '+%Y-%m-%d %H:%M %Z')"
echo ""

case "$CMD" in
    positions|pos|p)
        show_positions
        ;;
    scans|scan|s)
        show_scans
        ;;
    journal|j)
        show_journal
        ;;
    csv|c)
        show_csv
        ;;
    logs|log|l)
        show_logs
        ;;
    all|"")
        show_positions
        echo ""
        show_scans
        echo ""
        show_journal
        echo ""
        show_logs
        ;;
    *)
        echo "Unknown command: $CMD"
        echo "Usage: bash vps-status.sh [positions|scans|journal|csv|logs]"
        exit 1
        ;;
esac

echo ""
echo "═══════════════════════════════════════════════════════════════"
