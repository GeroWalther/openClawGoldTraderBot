#!/bin/bash
# Trade monitor — runs every 5min during market hours.
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

# Track which instruments have open positions (for runner state cleanup)
OPEN_INSTRUMENTS=""

# Process each position
_POSITION_LINES=$(echo "$json" | python3 -c "
import sys, json

d = json.load(sys.stdin)
positions = d.get('positions', [])
pending = d.get('pending_orders', [])
recent_trades = d.get('recent_trades', [])

# Build lookup: instrument+direction -> trade info (for runner detection)
trade_lookup = {}
for t in recent_trades:
    key = (t.get('epic', ''), t.get('direction', ''))
    if key not in trade_lookup:
        trade_lookup[key] = t

# Deduplicate positions by instrument+direction (aggregate size/pnl, keep first entry)
merged = {}
for p in positions:
    inst = p.get('instrument', p.get('contract', ''))
    direction = p.get('direction', p.get('side', ''))
    key = (inst, direction)
    if key not in merged:
        merged[key] = dict(p)
    else:
        m = merged[key]
        m['size'] = m.get('size', 0) + p.get('size', p.get('quantity', 0))
        m['unrealized_pnl'] = (m.get('unrealized_pnl', m.get('pnl', 0)) or 0) + (p.get('unrealized_pnl', p.get('pnl', 0)) or 0)

# Output position data for bash processing
for (inst, direction), p in merged.items():
    size = p.get('size', p.get('quantity', 0))
    entry = p.get('entry_price', p.get('avg_cost', 0))
    current = p.get('current_price', p.get('market_price', 0))
    pnl = p.get('unrealized_pnl', p.get('pnl', 0))
    sl = p.get('stop_loss', 0) or 0
    tp = p.get('take_profit', 0) or 0

    # Check if this is a ratchet position (m5_scalp with no TP)
    matching_trade = trade_lookup.get((inst, direction))
    is_ratchet = False
    stop_distance = 0
    if matching_trade:
        strategy = matching_trade.get('strategy', '')
        trade_tp = matching_trade.get('take_profit')
        stop_distance = matching_trade.get('stop_distance', 0) or 0
        if strategy == 'm5_scalp' and (trade_tp is None or trade_tp == 0):
            is_ratchet = True

    if is_ratchet:
        print(f'{inst}|{direction}|{size}|{entry}|{current}|{pnl}|{sl}|{tp}|0.0|ratchet_trail|{stop_distance}')
        continue

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

    print(f'{inst}|{direction}|{size}|{entry}|{current}|{pnl}|{sl}|{tp}|{pct_to_tp:.1f}|{action}|0')

# Also output pending orders for age check
for o in pending:
    oinst = o.get('instrument', o.get('contract', ''))
    odir = o.get('direction', o.get('side', ''))
    osource = o.get('source', '')
    ocreated = o.get('created_at', '')
    print(f'PENDING|{oinst}|{odir}|{osource}|{ocreated}|||||0.0|pending|0')
" 2>/dev/null
)

while IFS='|' read -r inst direction size entry current pnl sl tp pct_to_tp action stop_dist; do
    if [ -z "$inst" ]; then
        continue
    fi

    # Track open instrument+direction for runner state cleanup
    if [ "$inst" != "PENDING" ]; then
        OPEN_INSTRUMENTS="${OPEN_INSTRUMENTS}${inst}_${direction} "
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
    if [ "$action" = "ratchet_trail" ]; then
        # Ratchet SL: tighten by 0.5×sl_dist each time price moves 1R in profit
        state_file="$JOURNAL_DIR/monitors/ratchet_${inst}_${direction}.json"

        if [ ! -f "$state_file" ]; then
            # First detection: create state file, no SL move yet
            log "MONITOR $inst: Ratchet init — sl_dist=$stop_dist entry=$entry"
            python3 -c "
import json
state = {
    'instrument': '$inst',
    'direction': '$direction',
    'entry_price': float('$entry'),
    'sl_dist': float('$stop_dist'),
    'last_ratchet_level': 0
}
with open('$state_file', 'w') as f:
    json.dump(state, f, indent=2)
" 2>/dev/null
            log "MONITOR $inst: Ratchet state file created"
        else
            # Subsequent run: check if price moved another R, tighten SL
            trail_result=$(python3 -c "
import json, sys, math

with open('$state_file') as f:
    state = json.load(f)

direction = state['direction']
entry_price = state['entry_price']
sl_dist = state['sl_dist']
last_level = state.get('last_ratchet_level', 0)
current = float('$current')
current_sl = float('$sl') if float('$sl') != 0 else (entry_price - sl_dist if direction == 'BUY' else entry_price + sl_dist)

if sl_dist <= 0:
    print('ERROR')
    sys.exit()

# Calculate current profit in R-multiples
if direction == 'BUY':
    profit_r = (current - entry_price) / sl_dist
else:
    profit_r = (entry_price - current) / sl_dist

# Ratchet level = floor of profit R (only positive)
ratchet_level = max(0, int(math.floor(profit_r)))

# Calculate new SL: entry + ratchet_level × 0.5 × sl_dist
if ratchet_level > last_level and ratchet_level >= 1:
    if direction == 'BUY':
        new_sl = entry_price + ratchet_level * 0.5 * sl_dist
        should_move = new_sl > current_sl
    else:
        new_sl = entry_price - ratchet_level * 0.5 * sl_dist
        should_move = new_sl < current_sl

    if should_move and abs(new_sl - current_sl) > 1e-7:
        state['last_ratchet_level'] = ratchet_level
        with open('$state_file', 'w') as f:
            json.dump(state, f, indent=2)
        locked_r = ratchet_level * 0.5
        print(f'TRAIL|{new_sl:.6f}|{ratchet_level}|{locked_r:.1f}|{profit_r:.1f}')
    else:
        print(f'HOLD|{current_sl:.6f}|{last_level}|{last_level * 0.5:.1f}|{profit_r:.1f}')
else:
    print(f'HOLD|{current_sl:.6f}|{last_level}|{last_level * 0.5:.1f}|{profit_r:.1f}')
" 2>/dev/null || echo "ERROR")

            trail_action=$(echo "$trail_result" | cut -d'|' -f1)
            trail_new_sl=$(echo "$trail_result" | cut -d'|' -f2)
            trail_level=$(echo "$trail_result" | cut -d'|' -f3)
            trail_locked_r=$(echo "$trail_result" | cut -d'|' -f4)
            trail_profit_r=$(echo "$trail_result" | cut -d'|' -f5)

            if [ "$trail_action" = "TRAIL" ]; then
                log "MONITOR $inst: Ratchet SL to $trail_new_sl (${trail_level}R reached, locking ${trail_locked_r}R, current ${trail_profit_r}R)"
                trail_payload=$(python3 -c "
import json
print(json.dumps({
    'instrument': '$inst',
    'direction': '$direction',
    'new_stop_loss': float('$trail_new_sl'),
    'reasoning': 'Ratchet: ${trail_level}R reached, locking ${trail_locked_r}R profit'
}))
" 2>/dev/null || echo "")
                if [ -n "$trail_payload" ]; then
                    modify_result=$(api_post "/api/v1/positions/modify" "$trail_payload" 2>&1) || true
                    modify_status=$(echo "$modify_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','UNKNOWN'))" 2>/dev/null || echo "UNKNOWN")
                    log "MONITOR $inst: Ratchet result=$modify_status"
                    if [ "$modify_status" = "modified" ]; then
                        send_telegram "📈 Ratchet *$inst*: SL moved to $trail_new_sl (${trail_level}R hit, locking ${trail_locked_r}R profit)"
                    fi
                fi
            else
                log "MONITOR $inst: Ratchet holding (SL=$trail_new_sl, level=${trail_level}R, locked=${trail_locked_r}R, current=${trail_profit_r}R)"
            fi
        fi
    elif [ "$action" = "trail_sl" ]; then
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
print(f'{new_sl:.6f}')
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
done <<< "$_POSITION_LINES"

# Cleanup stale ratchet state files for positions that no longer exist
for state_file in "$JOURNAL_DIR"/monitors/ratchet_*.json; do
    [ -f "$state_file" ] || continue
    basename=$(basename "$state_file" .json)
    inst_dir="${basename#ratchet_}"
    if ! echo "$OPEN_INSTRUMENTS" | grep -q "$inst_dir"; then
        log "MONITOR: Cleaning up stale ratchet state: $basename"
        rm -f "$state_file"
    fi
done
# Also clean up old runner state files
for state_file in "$JOURNAL_DIR"/monitors/runner_*.json; do
    [ -f "$state_file" ] || continue
    rm -f "$state_file"
done

log "MONITOR done"
