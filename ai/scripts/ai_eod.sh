#!/bin/bash
# ai_eod.sh — Friday end-of-day manager. Runs Friday 20:55 UTC.
# Per-position decision: close intraday trades, hold swing/position if AI says so.

SCRIPT_NAME="eod"
source "$(dirname "$0")/ai_common.sh"

acquire_lock "eod" || exit 1
# EOD is Friday 20:55 UTC — inside market hours by definition, but gate for safety
gate_market_hours

log "Friday EOD manager starting"

STATUS_JSON=$(api_get "/api/v1/positions/status")
if [ -z "$STATUS_JSON" ]; then
    log "ERROR: failed to fetch positions"
    exit 1
fi

NUM_POSITIONS=$(echo "$STATUS_JSON" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('positions', [])))")
if [ "$NUM_POSITIONS" = "0" ]; then
    log "No positions to evaluate"
    exit 0
fi

REGIME=$(read_regime)

# Build context asking AI to decide per-position
USER_MSG=$(python3 -c "
import json
ctx = {
    'positions': json.loads('''$STATUS_JSON''').get('positions', []),
    'recent_trades': json.loads('''$STATUS_JSON''').get('recent_trades', []),
    'regime': json.loads('''$REGIME'''),
}
print('FRIDAY EOD DECISION — $(date -u '+%Y-%m-%d %H:%M UTC')')
print('')
print('For each open position, decide: close_now, hold_weekend')
print('The hold_style flag in the original trade reasoning (intraday/swing/position) is your default, but re-evaluate given current structure.')
print('BTC: always hold (24/7 market).')
print('FX/Gold: close intraday, hold swing ONLY if thesis intact AND position is in profit OR at structural support.')
print('')
print('Context:')
print(json.dumps(ctx, indent=2, default=str))
print('')
print('Output JSON: {\"positions\": [{\"instrument\":\"X\", \"direction\":\"BUY\", \"action\":\"close_now|hold_weekend\", \"reasoning\":\"...\"}]}')
" 2>/dev/null)

RESULT=$(call_claude "opus" "$PROMPTS_DIR/manager.md" "$USER_MSG")

if [ -z "$RESULT" ] || echo "$RESULT" | grep -q '"error"'; then
    log "EOD AI decision failed — falling back to close all non-BTC"
    # Fallback: close everything except BTC
    CLOSE_LIST=$(echo "$STATUS_JSON" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for p in d.get('positions', []):
    inst = p.get('instrument', '')
    direction = p.get('direction', '')
    if inst and direction and inst != 'BTC':
        print(f'{inst}|{direction}')
")
else
    # Parse AI decisions
    CLOSE_LIST=$(echo "$RESULT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    for p in d.get('positions', []):
        if p.get('action') == 'close_now':
            print(f\"{p.get('instrument','')}|{p.get('direction','')}\")
except Exception as e:
    print(f'# parse error: {e}', file=sys.stderr)
")
fi

TIMESTAMP=$(date -u '+%Y%m%d_%H%M')
echo "$RESULT" > "$AI_LOG_DIR/eod_${TIMESTAMP}.json"

if [ -z "$CLOSE_LIST" ]; then
    log "AI elected to hold all positions over the weekend"
    send_telegram "🌙 *EOD Friday* — AI holding all positions through weekend"
    exit 0
fi

HELD_LIST=$(echo "$RESULT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    for p in d.get('positions', []):
        if p.get('action') == 'hold_weekend':
            print(f\"{p.get('instrument','')} {p.get('direction','')}\")
except Exception:
    pass
" 2>/dev/null)

while IFS='|' read -r inst direction; do
    [ -z "$inst" ] && continue

    if is_paper_instrument "$inst"; then
        log "PAPER: would close $inst $direction at EOD"
        continue
    fi

    PAYLOAD=$(python3 -c "
import json
print(json.dumps({
    'instrument': '$inst',
    'direction': '$direction',
    'reasoning': 'AI EOD close: intraday position, weekend gap risk'
}))
")
    RESULT=$(api_post "/api/v1/positions/close" "$PAYLOAD")
    log "Close $inst $direction: $RESULT"
done <<< "$CLOSE_LIST"

CLOSED_COUNT=$(echo "$CLOSE_LIST" | grep -cv '^$')
HELD_COUNT=$(echo "$HELD_LIST" | grep -cv '^$')
send_telegram "🌙 *EOD Friday* — AI closed $CLOSED_COUNT, holding $HELD_COUNT over weekend$([ -n "$HELD_LIST" ] && echo "

Held: $HELD_LIST")"
log "EOD complete: closed=$CLOSED_COUNT, held=$HELD_COUNT"
