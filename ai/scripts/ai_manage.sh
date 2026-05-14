#!/bin/bash
# ai_manage.sh — AI position manager. Runs every 15 minutes.
# Reviews each open position via TradingView charts and decides: hold, trail_sl, move_to_be, partial_close, close.

SCRIPT_NAME="manage"
source "$(dirname "$0")/ai_common.sh"

acquire_lock "manage" || exit 1
gate_market_hours

log "Position management cycle starting"

# Fetch open positions
STATUS_JSON=$(api_get "/api/v1/positions/status")
if [ -z "$STATUS_JSON" ]; then
    log "ERROR: failed to fetch /positions/status"
    exit 1
fi

# Auto-cancel stale pending orders (>4h old) — prevents blocking new trades
STALE_CANCELS=$(echo "$STATUS_JSON" | python3 -c "
import sys, json
from datetime import datetime, timezone
d = json.load(sys.stdin)
now = datetime.now(timezone.utc)
for t in d.get('recent_trades', []):
    if t.get('status') not in ('PENDING_ORDER', 'pending_order'):
        continue
    created = t.get('created_at', '')
    if not created:
        continue
    try:
        c = datetime.fromisoformat(created.replace('Z', '+00:00'))
        if c.tzinfo is None:
            c = c.replace(tzinfo=timezone.utc)
        age_h = (now - c).total_seconds() / 3600
        if age_h > 4:
            inst = t.get('epic', '')
            direction = t.get('direction', '')
            print(f'{inst}|{direction}|{age_h:.1f}')
    except Exception:
        pass
" 2>/dev/null)

if [ -n "$STALE_CANCELS" ]; then
    while IFS='|' read -r inst direction age; do
        [ -z "$inst" ] && continue
        log "Auto-cancelling stale pending order: $inst $direction (${age}h old)"
        CANCEL_PAYLOAD=$(python3 -c "import json; print(json.dumps({'instrument':'$inst','direction':'$direction'}))")
        api_post "/api/v1/trades/cancel" "$CANCEL_PAYLOAD" > /dev/null 2>&1 || true
        send_telegram "🗑 Cancelled stale *$inst* $direction pending order (${age}h old)"
    done <<< "$STALE_CANCELS"
fi

NUM_POSITIONS=$(echo "$STATUS_JSON" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('positions', [])))")

if [ "$NUM_POSITIONS" = "0" ]; then
    log "No open positions — skipping"
    exit 0
fi

log "Managing $NUM_POSITIONS open position(s)"

REGIME=$(read_regime)

USER_MSG=$(python3 -c "
import json
ctx = {
    'positions': json.loads('''$STATUS_JSON''').get('positions', []),
    'regime': json.loads('''$REGIME'''),
}
print('Current UTC time: $(date -u '+%Y-%m-%d %H:%M:%S')')
print('')
print('OPEN POSITIONS:')
print(json.dumps(ctx, indent=2))
print('')
print('For each position: read the current chart via TradingView MCP, assess thesis, decide action.')
print('Output ONLY the JSON per the system prompt.')
" 2>/dev/null)

# Sonnet is fine for management — it's per-position assessment
RESULT=$(call_claude "sonnet" "$PROMPTS_DIR/manager.md" "$USER_MSG")

if [ -z "$RESULT" ] || echo "$RESULT" | grep -q '"error"'; then
    log "Management failed: $RESULT"
    exit 1
fi

# Archive
TIMESTAMP=$(date -u '+%Y%m%d_%H%M')
echo "$RESULT" > "$AI_LOG_DIR/manage_${TIMESTAMP}.json"

# Process each position's decision
NUM_DECISIONS=$(echo "$RESULT" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('positions', [])))")

for i in $(seq 0 $((NUM_DECISIONS - 1))); do
    POS=$(echo "$RESULT" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin)['positions'][$i]))")
    INST=$(echo "$POS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('instrument',''))")
    DIR=$(echo "$POS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('direction',''))")
    ACTION=$(echo "$POS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('action',''))")
    REASONING=$(echo "$POS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('reasoning',''))")
    TG_NOTE=$(echo "$POS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('telegram_note',''))")

    log "$INST $DIR: action=$ACTION — ${REASONING:0:100}"

    if [ "$ACTION" = "hold" ]; then
        continue
    fi

    if is_paper_instrument "$INST"; then
        log "$INST $DIR: PAPER MODE — would have executed $ACTION"
        continue
    fi

    case "$ACTION" in
        trail_sl|move_to_be)
            NEW_SL=$(echo "$POS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('new_stop_loss',''))")
            if [ -z "$NEW_SL" ]; then
                log "$INST: $ACTION but no new_stop_loss — skipping"
                continue
            fi
            MODIFY_PAYLOAD=$(python3 -c "
import json
print(json.dumps({
    'instrument': '$INST',
    'direction': '$DIR',
    'new_stop_loss': float('$NEW_SL'),
    'reasoning': '''AI manage: ${REASONING:0:300}'''
}))
")
            MODIFY_RESULT=$(api_post "/api/v1/positions/modify" "$MODIFY_PAYLOAD")
            log "$INST: modify result: $MODIFY_RESULT"
            [ -n "$TG_NOTE" ] && send_telegram "🤖 *$INST* $TG_NOTE"
            ;;

        close)
            CLOSE_PAYLOAD=$(python3 -c "
import json
print(json.dumps({
    'instrument': '$INST',
    'direction': '$DIR',
    'reasoning': '''AI manage: ${REASONING:0:300}'''
}))
")
            CLOSE_RESULT=$(api_post "/api/v1/positions/close" "$CLOSE_PAYLOAD")
            log "$INST: close result: $CLOSE_RESULT"
            [ -n "$TG_NOTE" ] && send_telegram "🤖 *$INST* $TG_NOTE"
            ;;

        partial_close)
            # Close half the position
            SIZE=$(echo "$STATUS_JSON" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for p in d.get('positions', []):
    if p.get('instrument') == '$INST' and p.get('direction') == '$DIR':
        print(abs(p.get('size', 0)) / 2)
        break
")
            CLOSE_PAYLOAD=$(python3 -c "
import json
print(json.dumps({
    'instrument': '$INST',
    'direction': '$DIR',
    'size': float('$SIZE'),
    'reasoning': '''AI manage partial: ${REASONING:0:300}'''
}))
")
            CLOSE_RESULT=$(api_post "/api/v1/positions/close" "$CLOSE_PAYLOAD")
            log "$INST: partial close result: $CLOSE_RESULT"
            [ -n "$TG_NOTE" ] && send_telegram "🤖 *$INST* $TG_NOTE"
            ;;

        *)
            log "$INST: unknown action '$ACTION' — skipping"
            ;;
    esac
done

log "Position management cycle complete"
