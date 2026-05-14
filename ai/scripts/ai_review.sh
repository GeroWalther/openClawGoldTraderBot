#!/bin/bash
# ai_review.sh — AI daily reviewer. Runs at 21:00 UTC.
# Reviews today's trades, updates insights, sends Telegram summary.

SCRIPT_NAME="review"
source "$(dirname "$0")/ai_common.sh"

acquire_lock "review" || exit 1
gate_market_hours

DATE=$(date -u '+%Y-%m-%d')
log "Daily review starting for $DATE"

# Update paper P&L history first (so reviewer sees closed paper trades)
if [ "$AI_PAPER_MODE" = "true" ]; then
    log "Updating paper P&L history..."
    "$PROJECT_DIR/.venv/bin/python3" "$AI_DIR/scripts/paper_pnl.py" --force 2>&1 | tail -20 | sed "s/^/  /" | tee -a "$AI_LOG_FILE" || log "paper_pnl.py failed (non-fatal)"
fi

# Fetch today's data
TRADES=$(api_get "/api/v1/journal?from_date=$DATE" || echo "[]")
ANALYTICS=$(api_get "/api/v1/analytics?from_date=$DATE" || echo "{}")
POSITIONS=$(api_get "/api/v1/positions/status" || echo "{}")
INSIGHTS=$(read_insights)

# Collect today's scan/regime archives
SCANS_TODAY=$(find "$AI_LOG_DIR" -name "scan_${DATE//-/}*.json" -type f 2>/dev/null | head -20 | xargs -I {} sh -c 'echo "--- {} ---"; cat {}' 2>/dev/null || echo "")
REGIMES_TODAY=$(find "$AI_LOG_DIR" -name "regime_${DATE//-/}*.json" -type f 2>/dev/null | head -20 | xargs -I {} sh -c 'echo "--- {} ---"; cat {}' 2>/dev/null || echo "")

USER_MSG=$(python3 -c "
import json
ctx = {
    'date': '$DATE',
    'trades_today': json.loads('''$TRADES''') if '''$TRADES''' else [],
    'analytics': json.loads('''$ANALYTICS''') if '''$ANALYTICS''' else {},
    'positions_open': json.loads('''$POSITIONS''').get('positions', []) if '''$POSITIONS''' else [],
    'previous_insights': json.loads('''$INSIGHTS''').get('insights', []),
}
print('Today: $DATE')
print('')
print('CONTEXT:')
print(json.dumps(ctx, indent=2, default=str))
print('')
print('Review today. Extract lessons. Output ONLY the JSON per the system prompt.')
" 2>/dev/null)

RESULT=$(call_claude "opus" "$PROMPTS_DIR/reviewer.md" "$USER_MSG")

if [ -z "$RESULT" ] || echo "$RESULT" | grep -q '"error"'; then
    log "Review failed: $RESULT"
    exit 1
fi

# Archive review
echo "$RESULT" > "$AI_LOG_DIR/review_${DATE}.json"

# Update insights
NEW_INSIGHTS=$(echo "$RESULT" | python3 -c "
import sys, json
r = json.load(sys.stdin)

# Load existing insights
try:
    with open('$INSIGHTS_FILE') as f:
        existing = json.load(f).get('insights', [])
except Exception:
    existing = []

# Remove obsolete
remove_keys = set(r.get('insights_to_remove', []))
existing = [i for i in existing if i.get('key') not in remove_keys]

# Add new
for new in r.get('insights_to_add', []):
    # Replace if key exists
    existing = [i for i in existing if i.get('key') != new.get('key')]
    new['added_date'] = '$DATE'
    existing.append(new)

# Keep max 20
existing = existing[-20:]

print(json.dumps({'insights': existing, 'last_updated': '$DATE'}, indent=2))
" 2>/dev/null)

if [ -n "$NEW_INSIGHTS" ]; then
    write_insights "$NEW_INSIGHTS"
    log "Updated insights — now tracking $(echo "$NEW_INSIGHTS" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('insights',[])))")"
fi

# Send Telegram summary
TG_MSG=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('telegram_summary',''))" 2>/dev/null)

if [ -n "$TG_MSG" ]; then
    send_telegram "$TG_MSG"
fi

log "Daily review complete"
