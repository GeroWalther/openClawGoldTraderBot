#!/bin/bash
# ai_scan.sh — AI setup scanner. Runs every 15 minutes.
# Reads current regime, scans instruments_in_play for setups, triggers ai_trade.sh if found.

SCRIPT_NAME="scan"
source "$(dirname "$0")/ai_common.sh"

acquire_lock "scan" || exit 1
gate_market_hours

log "Starting setup scan"

# Read current regime
REGIME=$(read_regime)
IN_PLAY=$(echo "$REGIME" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(','.join(d.get('instruments_in_play', [])))
" 2>/dev/null)

if [ -z "$IN_PLAY" ] || [ "$IN_PLAY" = "" ]; then
    log "No instruments in play — skipping scan"
    exit 0
fi

log "Instruments in play: $IN_PLAY"

# Build user message with regime context
USER_MSG=$(cat <<EOF
Current UTC time: $(date -u '+%Y-%m-%d %H:%M:%S')

Current regime (from last regime detection):
$REGIME

Scan for high-probability setups on instruments_in_play ONLY.
Use TradingView MCP tools to read H1 + M15 for each.
Apply the Quality Gate rules from the system prompt — reject weak setups.
If unsure about a pattern, inject a Pine Script verification indicator from ai/pine/.

Return ONLY the JSON object per the system prompt format.
EOF
)

RESULT=$(call_claude "sonnet" "$PROMPTS_DIR/scanner.md" "$USER_MSG")

if [ -z "$RESULT" ] || echo "$RESULT" | grep -q '"error"'; then
    log "Scan failed: $RESULT"
    exit 1
fi

VALID=$(echo "$RESULT" | python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    assert 'setups_found' in d
    assert 'setups' in d
    print('ok')
except Exception as e:
    print(f'invalid: {e}')
" 2>/dev/null)

if [ "$VALID" != "ok" ]; then
    log "Scan output invalid: $VALID — raw: $RESULT"
    exit 1
fi

# Archive scan
TIMESTAMP=$(date -u '+%Y%m%d_%H%M')
echo "$RESULT" > "$AI_LOG_DIR/scan_${TIMESTAMP}.json"

SETUPS_FOUND=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print('yes' if d.get('setups_found') and d.get('setups') else 'no')")

if [ "$SETUPS_FOUND" != "yes" ]; then
    log "No setups found — scan complete"
    exit 0
fi

# Process each setup — invoke ai_trade.sh with the setup JSON
NUM_SETUPS=$(echo "$RESULT" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('setups', [])))")
log "Found $NUM_SETUPS setup(s) — triggering trade decisions"

for i in $(seq 0 $((NUM_SETUPS - 1))); do
    SETUP=$(echo "$RESULT" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin)['setups'][$i]))")
    INST=$(echo "$SETUP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('instrument',''))")
    log "Setup $((i+1))/$NUM_SETUPS: $INST — calling ai_trade.sh"

    # Pipe setup JSON to ai_trade.sh via stdin
    echo "$SETUP" | bash "$(dirname "$0")/ai_trade.sh" || log "ai_trade.sh failed for $INST"
done

log "Scan cycle complete"
