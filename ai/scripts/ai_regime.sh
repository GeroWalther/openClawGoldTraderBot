#!/bin/bash
# ai_regime.sh — AI regime detector. Runs every 30 minutes.
# Reads TradingView charts via MCP, classifies market regime per instrument,
# writes ai/state/regime.json for downstream scripts.

SCRIPT_NAME="regime"
source "$(dirname "$0")/ai_common.sh"

acquire_lock "regime" || exit 1
gate_market_hours

log "Starting regime detection"

# Build user message with context
USER_MSG=$(cat <<EOF
Current UTC time: $(date -u '+%Y-%m-%d %H:%M:%S')
Day of week: $(date -u '+%A')

Read TradingView charts for: XAUUSD, AUDUSD, NZDUSD, GBPUSD, BTCUSD.

For each, use the TradingView MCP tools to read D1/H4/H1 data, indicators, and S/R levels.
Then classify the regime and output the JSON per the system prompt.

Return ONLY the JSON object, no markdown, no commentary.
EOF
)

# Call Claude with Sonnet (fast + cost-effective for structured classification)
RESULT=$(call_claude "sonnet" "$PROMPTS_DIR/regime.md" "$USER_MSG")

if [ -z "$RESULT" ] || echo "$RESULT" | grep -q '"error"'; then
    log "Regime detection failed: $RESULT"
    exit 1
fi

# Validate it's valid JSON with expected fields
VALID=$(echo "$RESULT" | python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    assert 'instruments_in_play' in d
    assert 'instruments_avoid' in d
    print('ok')
except Exception as e:
    print(f'invalid: {e}')
" 2>/dev/null)

if [ "$VALID" != "ok" ]; then
    log "Regime output invalid JSON: $VALID"
    log "Raw output: $RESULT"
    exit 1
fi

# Write to state
write_regime "$RESULT"

# Archive per-run copy
TIMESTAMP=$(date -u '+%Y%m%d_%H%M')
echo "$RESULT" > "$AI_LOG_DIR/regime_${TIMESTAMP}.json"

# Log summary
SUMMARY=$(echo "$RESULT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
in_play = d.get('instruments_in_play', [])
avoid = d.get('instruments_avoid', [])
summary = d.get('summary', '')
print(f\"in_play={','.join(in_play) or 'NONE'} avoid={','.join(avoid) or 'NONE'} — {summary}\")
" 2>/dev/null)

log "Regime done: $SUMMARY"
