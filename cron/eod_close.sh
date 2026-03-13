#!/bin/bash
# End-of-day close — runs at 20:55 UTC, Mon-Fri.
# Closes all open positions to avoid overnight swap fees.

source "$(dirname "$0")/common.sh"

log "EOD CLOSE starting"

# Fetch open positions
json=$(api_get "/api/v1/positions/status")
if [ -z "$json" ]; then
    log "EOD CLOSE: Failed to fetch positions"
    exit 0
fi

positions=$(echo "$json" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for p in d.get('positions', []):
    inst = p.get('instrument', '')
    direction = p.get('direction', '')
    size = abs(p.get('size', 0))
    pnl = p.get('unrealized_pnl', 0) or 0
    print(f'{inst}|{direction}|{size}|{pnl}')
" 2>/dev/null)

if [ -z "$positions" ]; then
    log "EOD CLOSE: No open positions — nothing to close"
    exit 0
fi

closed=0
total_pnl=0

while IFS='|' read -r inst direction size pnl; do
    [ -z "$inst" ] && continue

    log "EOD CLOSE: Closing $direction $inst (size=$size, unrealized=$pnl)"

    close_payload=$(python3 -c "
import json
print(json.dumps({
    'instrument': '$inst',
    'direction': '$direction',
    'size': float('$size'),
    'reasoning': 'EOD auto-close: avoiding overnight swap fees'
}))
" 2>/dev/null || echo "")

    if [ -z "$close_payload" ]; then
        log "EOD CLOSE: Failed to build payload for $inst"
        continue
    fi

    result=$(api_post "/api/v1/positions/close" "$close_payload" 2>&1) || true
    if [ -n "$result" ]; then
        status=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','UNKNOWN'))" 2>/dev/null || echo "UNKNOWN")
        result_pnl=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('pnl', 0) or 0)" 2>/dev/null || echo "0")
        log "EOD CLOSE: $inst result=$status pnl=$result_pnl"
        closed=$((closed + 1))
        total_pnl=$(python3 -c "print(float('$total_pnl') + float('$result_pnl'))" 2>/dev/null || echo "$total_pnl")
    else
        log "EOD CLOSE: $inst FAILED — no response"
    fi
done <<< "$positions"

log "EOD CLOSE done — closed $closed position(s), total P&L: $total_pnl"

# Send summary via Telegram
if [ "$closed" -gt 0 ]; then
    send_telegram "🌙 EOD Auto-Close: $closed position(s) closed, P&L: ${total_pnl}€ (avoiding swap fees)"
fi
