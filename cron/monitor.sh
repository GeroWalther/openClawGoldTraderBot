#!/bin/bash
# Trade monitor — runs every 2h during market hours.
# Checks open positions and pending orders, alerts on important levels.

source "$(dirname "$0")/common.sh"

TIMESTAMP=$(date -u '+%Y-%m-%d_%H%M')
CSV_FILE="$JOURNAL_DIR/monitors/monitors.csv"

ensure_csv_header "$CSV_FILE" \
    "timestamp,instrument,direction,size,entry,current,pnl,stop_loss,take_profit,pct_to_tp,action"

log "MONITOR starting"

json=$(api_get "/api/v1/positions/status")
if [ -z "$json" ]; then
    log "MONITOR: Failed to fetch /positions/status"
    exit 1
fi

# Check if there are positions or pending orders
has_data=$(echo "$json" | python3 -c "
import sys, json
d = json.load(sys.stdin)
positions = d.get('positions', [])
pending = d.get('pending_orders', [])
print('yes' if (positions or pending) else 'no')
" 2>/dev/null || echo "no")

if [ "$has_data" = "no" ]; then
    log "MONITOR: No open positions or pending orders — skipping"
    exit 0
fi

# Save full JSON
echo "$json" > "$JOURNAL_DIR/monitors/${TIMESTAMP}.json"
echo "$json" > "$JOURNAL_DIR/latest_monitor.json"

# Process each position
echo "$json" | python3 -c "
import sys, json

d = json.load(sys.stdin)
positions = d.get('positions', [])
pending = d.get('pending_orders', [])

# Output position data for bash processing
for p in positions:
    inst = p.get('instrument', p.get('contract', ''))
    direction = p.get('direction', p.get('side', ''))
    size = p.get('size', p.get('quantity', 0))
    entry = p.get('entry_price', p.get('avg_cost', 0))
    current = p.get('current_price', p.get('market_price', 0))
    pnl = p.get('unrealized_pnl', p.get('pnl', 0))
    sl = p.get('stop_loss', 0) or 0
    tp = p.get('take_profit', 0) or 0

    # Calculate % to TP and % to SL
    pct_to_tp = 0
    pct_to_sl = 0
    action = 'hold'

    if entry and current and tp and tp != entry:
        if direction in ('BUY', 'LONG'):
            pct_to_tp = (current - entry) / (tp - entry) * 100 if tp > entry else 0
        else:
            pct_to_tp = (entry - current) / (entry - tp) * 100 if entry > tp else 0

    if entry and current and sl and sl != entry:
        if direction in ('BUY', 'LONG'):
            pct_to_sl = (entry - current) / (entry - sl) * 100 if entry > sl else 0
        else:
            pct_to_sl = (current - entry) / (sl - entry) * 100 if sl > entry else 0

    # Determine action — progressive SL management
    # 75%+ to TP: trail SL to lock in ~50% of profit
    # 50-74% to TP: move SL to breakeven
    # >70% toward SL: warn
    if pct_to_tp >= 75:
        action = 'trail_sl'
    elif pct_to_tp >= 50:
        action = 'move_sl_to_be'
    elif pct_to_sl >= 70:
        action = 'warn_near_sl'

    print(f'{inst}|{direction}|{size}|{entry}|{current}|{pnl}|{sl}|{tp}|{pct_to_tp:.1f}|{action}')

# Also output pending orders for age check
for o in pending:
    oinst = o.get('instrument', o.get('contract', ''))
    odir = o.get('direction', o.get('side', ''))
    osource = o.get('source', '')
    ocreated = o.get('created_at', '')
    print(f'PENDING|{oinst}|{odir}|{osource}|{ocreated}|||||0.0|pending')
" 2>/dev/null | while IFS='|' read -r inst direction size entry current pnl sl tp pct_to_tp action; do
    if [ -z "$inst" ]; then
        continue
    fi

    # Handle pending orders — check for expiration
    if [ "$inst" = "PENDING" ]; then
        p_inst="$direction"    # shifted columns for pending format
        p_dir="$size"
        p_source="$entry"
        p_created="$current"

        if [ -n "$p_created" ] && [ -n "$p_inst" ]; then
            # Calculate age in hours
            age_hours=$(python3 -c "
from datetime import datetime, timezone
try:
    created = datetime.fromisoformat('$p_created'.replace('Z', '+00:00'))
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - created).total_seconds() / 3600
    print(f'{age:.1f}')
except: print('0')
" 2>/dev/null || echo "0")

            # TTL: 4h for intraday orders, 24h for swing orders
            if echo "$p_source" | grep -q "intraday"; then
                max_hours="4"
            else
                max_hours="24"
            fi

            should_cancel=$(python3 -c "print('yes' if float('$age_hours') > float('$max_hours') else 'no')" 2>/dev/null || echo "no")
            if [ "$should_cancel" = "yes" ]; then
                log "MONITOR: Cancelling stale pending order $p_inst $p_dir (age=${age_hours}h, max=${max_hours}h)"
                cancel_payload=$(python3 -c "
import json
print(json.dumps({'instrument': '$p_inst', 'direction': '$p_dir'}))
" 2>/dev/null || echo "")
                if [ -n "$cancel_payload" ]; then
                    cancel_result=$(api_post "/api/v1/orders/cancel" "$cancel_payload" 2>&1) || true
                    log "MONITOR: Cancel result: $cancel_result"
                    send_telegram "🗑 Cancelled stale *$p_inst* $p_dir pending order (${age_hours}h old)"
                fi
            else
                log "MONITOR: Pending $p_inst $p_dir age=${age_hours}h (max=${max_hours}h) — keeping"
            fi
        fi
        continue
    fi

    # Append CSV row
    echo "${TIMESTAMP},${inst},${direction},${size},${entry},${current},${pnl},${sl},${tp},${pct_to_tp},${action}" >> "$CSV_FILE"

    log "MONITOR $inst: dir=$direction pnl=$pnl pct_to_tp=${pct_to_tp}% action=$action"

    # Act on signals
    if [ "$action" = "trail_sl" ]; then
        # Trail SL to lock in ~50% of current profit
        trail_level=$(python3 -c "
entry = float('$entry')
current = float('$current')
direction = '$direction'
if direction in ('BUY', 'LONG'):
    profit = current - entry
    new_sl = entry + profit * 0.5
else:
    profit = entry - current
    new_sl = entry - profit * 0.5
print(f'{new_sl:.2f}')
" 2>/dev/null || echo "")
        if [ -n "$trail_level" ]; then
            # Only trail if new SL is better than current SL
            should_trail=$(python3 -c "
direction = '$direction'
new_sl = float('$trail_level')
current_sl = float('$sl') if '$sl' != '0' else None
if current_sl is None:
    print('yes')
elif direction in ('BUY', 'LONG'):
    print('yes' if new_sl > current_sl else 'no')
else:
    print('yes' if new_sl < current_sl else 'no')
" 2>/dev/null || echo "no")
            if [ "$should_trail" = "yes" ]; then
                log "MONITOR $inst: 75%+ to TP — trailing SL to $trail_level (lock ~50% profit)"
                trail_payload=$(python3 -c "
import json
print(json.dumps({
    'instrument': '$inst',
    'direction': '$direction',
    'new_stop_loss': float('$trail_level'),
    'reasoning': 'Auto trail: 75%+ to TP, locking 50% profit'
}))
" 2>/dev/null || echo "")
                if [ -n "$trail_payload" ]; then
                    trail_result=$(api_post "/api/v1/positions/modify" "$trail_payload" 2>&1) || true
                    trail_status=$(echo "$trail_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','UNKNOWN'))" 2>/dev/null || echo "UNKNOWN")
                    log "MONITOR $inst: Trail SL result=$trail_status"
                    if [ "$trail_status" = "modified" ]; then
                        send_telegram "📈 *$inst* SL trailed to $trail_level (locking 50% profit) — 75%+ to TP"
                    fi
                fi
            fi
        fi
    elif [ "$action" = "move_sl_to_be" ]; then
        log "MONITOR $inst: >50% to TP — moving SL to breakeven (entry=$entry)"
        modify_payload=$(python3 -c "
import json
print(json.dumps({
    'instrument': '$inst',
    'direction': '$direction',
    'new_stop_loss': float('$entry'),
    'reasoning': 'Auto SL-to-BE: >50% to TP'
}))
" 2>/dev/null || echo "")
        if [ -n "$modify_payload" ]; then
            modify_result=$(api_post "/api/v1/positions/modify" "$modify_payload" 2>&1) || true
            if [ -n "$modify_result" ]; then
                modify_status=$(echo "$modify_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','UNKNOWN'))" 2>/dev/null || echo "UNKNOWN")
                log "MONITOR $inst: SL-to-BE result=$modify_status"
                if [ "$modify_status" = "modified" ]; then
                    send_telegram "📊 *$inst* SL moved to breakeven ($entry) — 50%+ to TP"
                fi
            else
                log "MONITOR $inst: SL-to-BE FAILED — no response"
            fi
        fi
    elif [ "$action" = "warn_near_sl" ]; then
        log "MONITOR $inst: >70% toward SL — sending alert"
        send_telegram "⚠️ *$inst* $direction position near SL — PnL: $pnl, ${pct_to_tp}% to TP"
    fi
done

log "MONITOR done"
