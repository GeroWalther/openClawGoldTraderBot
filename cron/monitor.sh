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
echo "$json" | python3 -c "
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

    # Check if this is a runner position:
    # 1. No TP order (TP1 already filled, no TP2)
    # 2. Matching trade has strategy == 'm5_scalp'
    matching_trade = trade_lookup.get((inst, direction))
    is_runner = False
    stop_distance = 0
    if matching_trade:
        strategy = matching_trade.get('strategy', '')
        trade_tp = matching_trade.get('take_profit')
        stop_distance = matching_trade.get('stop_distance', 0) or 0
        # Runner: m5_scalp with no TP2 in DB, and no TP order on position
        if strategy == 'm5_scalp' and (trade_tp is None or trade_tp == 0) and (tp == 0):
            is_runner = True

    if is_runner:
        # Output runner line with stop_distance as extra field
        print(f'{inst}|{direction}|{size}|{entry}|{current}|{pnl}|{sl}|{tp}|0.0|runner_trail|{stop_distance}')
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
" 2>/dev/null | while IFS='|' read -r inst direction size entry current pnl sl tp pct_to_tp action stop_dist; do
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
    if [ "$action" = "runner_trail" ]; then
        # Runner position: trail SL at 1R behind peak price
        state_file="$JOURNAL_DIR/monitors/runner_${inst}_${direction}.json"

        # Fetch current M5 ATR for trail distance (1 ATR behind peak)
        runner_atr=$(api_get "/api/v1/technicals/${inst}/m5scalp" | python3 -c "
import sys, json
d = json.load(sys.stdin)
atr = d.get('technicals', {}).get('m5', {}).get('atr', 0) or 0
print(f'{float(atr):.2f}')
" 2>/dev/null || echo "0")

        if [ ! -f "$state_file" ]; then
            # First detection: move SL to breakeven + resize SL quantity
            log "MONITOR $inst: Runner first detection — SL to BE + resize to $size (ATR=$runner_atr)"
            runner_payload=$(python3 -c "
import json
print(json.dumps({
    'instrument': '$inst',
    'direction': '$direction',
    'new_stop_loss': float('$entry'),
    'new_sl_quantity': abs(float('$size')),
    'reasoning': 'Runner: TP1 filled, SL to breakeven + resize'
}))
" 2>/dev/null || echo "")
            if [ -n "$runner_payload" ]; then
                runner_result=$(api_post "/api/v1/positions/modify" "$runner_payload" 2>&1) || true
                runner_status=$(echo "$runner_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','UNKNOWN'))" 2>/dev/null || echo "UNKNOWN")
                log "MONITOR $inst: Runner init result=$runner_status"
                if [ "$runner_status" = "modified" ]; then
                    # Create state file with current ATR as trail distance
                    python3 -c "
import json
atr = float('$runner_atr')
# Use current M5 ATR for trailing, fall back to stop_distance if ATR unavailable
trail_dist = atr if atr > 0 else float('$stop_dist')
state = {
    'instrument': '$inst',
    'direction': '$direction',
    'entry_price': float('$entry'),
    'r_distance': trail_dist,
    'peak_price': float('$current'),
    'sl_adjusted': True
}
with open('$state_file', 'w') as f:
    json.dump(state, f, indent=2)
" 2>/dev/null
                    send_telegram "🏃 Runner active for *$inst* $direction — SL moved to breakeven, trail=1 ATR ($runner_atr)"
                fi
            fi
        else
            # Subsequent run: update ATR + peak + trail SL (never move SL back)
            trail_result=$(python3 -c "
import json, sys

with open('$state_file') as f:
    state = json.load(f)

direction = state['direction']
peak_price = state['peak_price']
entry_price = state['entry_price']
current = float('$current')
current_sl = float('$sl') if float('$sl') != 0 else entry_price

# Use current M5 ATR for trail distance (adapts to volatility)
atr = float('$runner_atr')
r_distance = atr if atr > 0 else state['r_distance']

# Update peak
if direction == 'BUY':
    peak_price = max(peak_price, current)
else:
    peak_price = min(peak_price, current)

# Calculate trailing SL (1 ATR behind peak, never move back)
if direction == 'BUY':
    new_sl = max(entry_price, peak_price - r_distance)
    should_move = new_sl > current_sl
else:
    new_sl = min(entry_price, peak_price + r_distance)
    should_move = new_sl < current_sl

# Calculate profit in R-multiples (using original stop_distance for R calc)
orig_r = state['r_distance']
if orig_r > 0:
    if direction == 'BUY':
        profit_r = (new_sl - entry_price) / orig_r
    else:
        profit_r = (entry_price - new_sl) / orig_r
else:
    profit_r = 0

# Update state file with current ATR and peak
state['peak_price'] = peak_price
state['r_distance'] = r_distance
with open('$state_file', 'w') as f:
    json.dump(state, f, indent=2)

if should_move:
    print(f'TRAIL|{new_sl:.2f}|{peak_price:.2f}|{profit_r:.1f}|{r_distance:.0f}')
else:
    print(f'HOLD|{current_sl:.2f}|{peak_price:.2f}|{profit_r:.1f}|{r_distance:.0f}')
" 2>/dev/null || echo "ERROR")

            trail_action=$(echo "$trail_result" | cut -d'|' -f1)
            trail_new_sl=$(echo "$trail_result" | cut -d'|' -f2)
            trail_peak=$(echo "$trail_result" | cut -d'|' -f3)
            trail_profit_r=$(echo "$trail_result" | cut -d'|' -f4)
            trail_atr=$(echo "$trail_result" | cut -d'|' -f5)

            if [ "$trail_action" = "TRAIL" ]; then
                log "MONITOR $inst: Runner trailing SL to $trail_new_sl (peak=$trail_peak, ATR=$trail_atr, locking ${trail_profit_r}R)"
                trail_payload=$(python3 -c "
import json
print(json.dumps({
    'instrument': '$inst',
    'direction': '$direction',
    'new_stop_loss': float('$trail_new_sl'),
    'reasoning': 'Runner trail: peak=$trail_peak, 1 ATR=$trail_atr, locking ${trail_profit_r}R'
}))
" 2>/dev/null || echo "")
                if [ -n "$trail_payload" ]; then
                    modify_result=$(api_post "/api/v1/positions/modify" "$trail_payload" 2>&1) || true
                    modify_status=$(echo "$modify_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','UNKNOWN'))" 2>/dev/null || echo "UNKNOWN")
                    log "MONITOR $inst: Runner trail result=$modify_status"
                    if [ "$modify_status" = "modified" ]; then
                        send_telegram "🏃 Runner *$inst*: SL trailed to $trail_new_sl (peak $trail_peak, 1ATR=$trail_atr, ${trail_profit_r}R locked)"
                    fi
                fi
            else
                log "MONITOR $inst: Runner holding (SL=$trail_new_sl, peak=$trail_peak, ATR=$trail_atr, ${trail_profit_r}R)"
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

# Cleanup stale runner state files for positions that no longer exist
for state_file in "$JOURNAL_DIR"/monitors/runner_*.json; do
    [ -f "$state_file" ] || continue
    # Extract instrument_direction from filename: runner_BTC_BUY.json -> BTC_BUY
    basename=$(basename "$state_file" .json)
    inst_dir="${basename#runner_}"
    if ! echo "$OPEN_INSTRUMENTS" | grep -q "$inst_dir"; then
        log "MONITOR: Cleaning up stale runner state: $basename"
        rm -f "$state_file"
    fi
done

log "MONITOR done"
