#!/bin/bash
# ai_trade.sh — AI trade decision maker.
# Called by ai_scan.sh with setup JSON on stdin. Decides BUY/SELL/PASS and executes via gold-trader API.

SCRIPT_NAME="trade"
source "$(dirname "$0")/ai_common.sh"

# Read setup from stdin
SETUP=$(cat)
if [ -z "$SETUP" ]; then
    log "ERROR: no setup on stdin"
    exit 1
fi

INST=$(echo "$SETUP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('instrument',''))")
if [ -z "$INST" ]; then
    log "ERROR: setup missing instrument"
    exit 1
fi

log "Trade decision for $INST starting"

# Gather full context
REGIME=$(read_regime)
# Only fetch what the trader NEEDS — no journal (prevents PASS-counting behavior)
POSITIONS=$(api_get "/api/v1/positions/status" || echo "{}")
ACCOUNT=$(api_get "/api/v1/positions/account" || echo "{}")

# Build focused user message — setup + regime + positions only
# Deliberately EXCLUDED: journal_recent (Claude counts PASSes and locks itself out)
# Deliberately EXCLUDED: insights (too many guidelines → paralysis)
USER_MSG=$(python3 -c "
import json, sys
setup = json.loads('''$SETUP''')
regime = json.loads('''$REGIME''')
positions = json.loads('''$POSITIONS''') if '''$POSITIONS''' else {}

# Only pass open positions list (not recent_trades which has PASS history)
open_positions = positions.get('positions', [])
account = json.loads('''$ACCOUNT''') if '''$ACCOUNT''' else {}

ctx = {
    'setup': setup,
    'regime': regime,
    'open_positions': open_positions,
    'account_balance': account.get('account', {}).get('NetLiquidation', 0),
}
print('Current UTC time: $(date -u '+%Y-%m-%d %H:%M:%S')')
print('')
print('SETUP TO VERIFY:')
print(json.dumps(ctx, indent=2))
print('')
print('Verify this setup via TradingView MCP. If valid and R:R >= 1:2, TRADE. If invalidated, PASS.')
print('Output ONLY the JSON.')
" 2>/dev/null)

# Use OPUS for trade decisions — critical path, worth the cost
RESULT=$(call_claude "opus" "$PROMPTS_DIR/trader.md" "$USER_MSG")

if [ -z "$RESULT" ] || echo "$RESULT" | grep -q '"error"'; then
    log "$INST: trade decision failed: $RESULT"
    exit 1
fi

# Validate
ACTION=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('action',''))" 2>/dev/null)

if [ -z "$ACTION" ]; then
    log "$INST: invalid trade decision output: $RESULT"
    exit 1
fi

# Archive decision
TIMESTAMP=$(date -u '+%Y%m%d_%H%M%S')
echo "$RESULT" > "$STATE_DIR/trade_reasoning/${TIMESTAMP}_${INST}_${ACTION}.json"

REASONING=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('reasoning',''))" 2>/dev/null)
log "$INST: decision=$ACTION — ${REASONING:0:100}"

if [ "$ACTION" = "PASS" ]; then
    # Journal the no-trade decision for the reviewer
    api_post "/api/v1/journal" "$(python3 -c "
import json
d = {
    'instrument': '$INST',
    'direction': 'NO_TRADE',
    'total_score': 0,
    'factors': {},
    'reasoning': '''${REASONING:0:500}''',
    'source': 'ai_trader_pass'
}
print(json.dumps(d))
")" > /dev/null 2>&1 || true
    log "$INST: PASS logged — no trade"
    exit 0
fi

# Per-instrument paper/live check
if is_paper_instrument "$INST"; then
    log "$INST: PAPER (not in AI_LIVE_INSTRUMENTS) — would have submitted $ACTION trade"
    send_telegram "📝 *PAPER* $INST $ACTION — $(echo "$REASONING" | head -c 300)"
    exit 0
fi

log "$INST: LIVE — submitting $ACTION trade"

# Build trade submission payload
PAYLOAD=$(echo "$RESULT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
payload = {
    'instrument': d['instrument'],
    'direction': d['action'],
    'order_type': d.get('order_type', 'MARKET'),
    'conviction': d.get('conviction', 'MEDIUM'),
    'source': 'ai_trader',
    'reasoning': d.get('reasoning', '')[:500],
    'strategy': d.get('strategy', 'ai_discretionary'),
}
if d.get('order_type') in ('LIMIT', 'STOP'):
    payload['entry_price'] = d['entry_price']
if 'stop_distance' in d:
    payload['stop_distance'] = d['stop_distance']
if 'limit_distance' in d:
    payload['limit_distance'] = d['limit_distance']
if 'stop_loss' in d and 'stop_distance' not in d:
    payload['stop_level'] = d['stop_loss']
if 'take_profit' in d and 'limit_distance' not in d:
    payload['limit_level'] = d['take_profit']
# Embed hold_style in reasoning for ai_eod.sh and ai_manage.sh to read
hold_style = d.get('hold_style', 'intraday')
payload['reasoning'] = f\"[{hold_style}] {payload['reasoning']}\"
print(json.dumps(payload))
" 2>/dev/null)

if [ -z "$PAYLOAD" ]; then
    log "$INST: failed to build trade payload"
    exit 1
fi

log "$INST: submitting trade — $PAYLOAD"

TRADE_RESULT=$(api_post "/api/v1/trades/submit" "$PAYLOAD")
STATUS=$(echo "$TRADE_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','UNKNOWN'))" 2>/dev/null || echo "UNKNOWN")
MSG=$(echo "$TRADE_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('message',''))" 2>/dev/null || echo "")

log "$INST: trade result=$STATUS — $MSG"

# Telegram notification is already sent by the bot's TradeExecutor.
# We add a Krabbe-flavored note:
send_telegram "🤖 *Krabbe AI Trade* — $INST $ACTION
Status: $STATUS
$MSG

*Reasoning:* $(echo "$REASONING" | head -c 300)"
