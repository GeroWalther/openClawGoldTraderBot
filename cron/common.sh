#!/bin/bash
# Shared helpers for all cron scripts.
# Source this file: source "$(dirname "$0")/common.sh"

set -euo pipefail

# --- Config ---
ENV_FILE="/opt/gold-trader/.env.production"
JOURNAL_DIR="/opt/gold-trader/journal"
LOG_FILE="$JOURNAL_DIR/cron.log"
BOT_URL="http://localhost:8001"

# Load environment
if [ -f "$ENV_FILE" ]; then
    TG_TOKEN=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
    TG_CHAT_ID=$(grep -E '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | cut -d= -f2-)
    API_KEY=$(grep -E '^API_SECRET_KEY=' "$ENV_FILE" | cut -d= -f2-)
else
    echo "ERROR: $ENV_FILE not found"
    exit 1
fi

if [ -z "$TG_TOKEN" ] || [ -z "$TG_CHAT_ID" ] || [ -z "$API_KEY" ]; then
    echo "ERROR: Missing TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, or API_SECRET_KEY in $ENV_FILE"
    exit 1
fi

# --- Ensure journal directories ---
mkdir -p "$JOURNAL_DIR/intraday/scans"
mkdir -p "$JOURNAL_DIR/swing/scans"
mkdir -p "$JOURNAL_DIR/scalp/scans"
mkdir -p "$JOURNAL_DIR/monitors"
mkdir -p "$JOURNAL_DIR/summaries"

# --- Instruments ---
INSTRUMENTS=("XAUUSD" "BTC")

# --- File locking (prevents concurrent script execution) ---
LOCK_DIR="/tmp/gold-trader-locks"
mkdir -p "$LOCK_DIR"

acquire_lock() {
    local name="$1"
    local lockfile="$LOCK_DIR/${name}.lock"
    local max_wait=30  # seconds

    for i in $(seq 1 "$max_wait"); do
        if mkdir "$lockfile" 2>/dev/null; then
            # Store PID for stale lock detection
            echo $$ > "$lockfile/pid"
            trap "rm -rf '$lockfile'" EXIT
            return 0
        fi
        # Check for stale lock (process no longer running)
        if [ -f "$lockfile/pid" ]; then
            local lock_pid
            lock_pid=$(cat "$lockfile/pid" 2>/dev/null || echo "0")
            if ! kill -0 "$lock_pid" 2>/dev/null; then
                rm -rf "$lockfile"
                continue
            fi
        fi
        sleep 1
    done
    echo "ERROR: Could not acquire lock '$name' after ${max_wait}s" >&2
    return 1
}

# --- Helpers ---

log() {
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*" >> "$LOG_FILE"
}

send_telegram() {
    local msg="$1"
    curl -s -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
        -d chat_id="$TG_CHAT_ID" \
        -d parse_mode="Markdown" \
        -d text="$msg" \
        -d disable_web_page_preview=true > /dev/null 2>&1 || true
}

api_get() {
    local endpoint="$1"
    curl -sf -H "x-api-key: $API_KEY" "${BOT_URL}${endpoint}" 2>/dev/null
}

api_post() {
    local endpoint="$1"
    local data="$2"
    curl -sf -X POST -H "x-api-key: $API_KEY" -H "Content-Type: application/json" \
        -d "$data" "${BOT_URL}${endpoint}" 2>/dev/null
}

# Ensure CSV has header if file doesn't exist
ensure_csv_header() {
    local file="$1"
    local header="$2"
    if [ ! -f "$file" ]; then
        echo "$header" > "$file"
    fi
}

# Cache for open positions/pending orders (fetched once per script run)
_POSITIONS_JSON=""

has_open_position() {
    local inst="$1"
    # Fetch once, cache for the rest of the script
    if [ -z "$_POSITIONS_JSON" ]; then
        _POSITIONS_JSON=$(api_get "/api/v1/positions/status" || echo "{}")
        if [ -z "$_POSITIONS_JSON" ] || [ "$_POSITIONS_JSON" = "{}" ]; then
            _POSITIONS_JSON='{"positions":[],"pending_orders":[]}'
        fi
    fi
    echo "$_POSITIONS_JSON" | python3 -c "
import sys, json
d = json.load(sys.stdin)
inst = '$inst'
for p in d.get('positions', []):
    if p.get('instrument', p.get('contract', '')) == inst:
        print('yes'); sys.exit()
for o in d.get('pending_orders', []):
    if o.get('instrument', o.get('contract', '')) == inst:
        print('yes'); sys.exit()
print('no')
" 2>/dev/null || echo "no"
}

# --- Correlation check ---
# Correlation groups: instruments within the same group are correlated.
# Prevents: double risk (same direction on correlated assets) and
# conflicting trades (opposite direction on correlated assets).
#
# Returns: "ok", "block_correlated" (same dir already open), or "block_conflict" (opposite dir)
check_correlation() {
    local inst="$1"
    local direction="$2"

    if [ -z "$_POSITIONS_JSON" ]; then
        _POSITIONS_JSON=$(api_get "/api/v1/positions/status" || echo "{}")
        if [ -z "$_POSITIONS_JSON" ] || [ "$_POSITIONS_JSON" = "{}" ]; then
            _POSITIONS_JSON='{"positions":[],"pending_orders":[]}'
        fi
    fi

    echo "$_POSITIONS_JSON" | python3 -c "
import sys, json

# Correlation groups — instruments in same group move together
CORRELATION_GROUPS = {
    'risk_on_usd_down': ['XAUUSD', 'EURUSD'],     # Both rise when USD weakens
    'risk_on_crypto':   ['BTC'],                     # Crypto — independent
    'jpy_pairs':        ['EURJPY', 'CADJPY', 'USDJPY'],  # JPY crosses
    'sp500':            ['MES', 'IBUS500'],          # S&P 500
}

# Inverse correlations — these pairs tend to move opposite
INVERSE_PAIRS = {
    ('XAUUSD', 'USDJPY'),  # Gold up = USD down = USDJPY down
    ('EURUSD', 'USDJPY'),  # EUR up = USD down = USDJPY down
}

d = json.load(sys.stdin)
inst = '$inst'
direction = '$direction'

# Get current open positions and pending orders
open_positions = []
for p in d.get('positions', []):
    open_positions.append({
        'instrument': p.get('instrument', p.get('contract', '')),
        'direction': p.get('direction', p.get('side', '')),
    })
for o in d.get('pending_orders', []):
    open_positions.append({
        'instrument': o.get('instrument', o.get('contract', '')),
        'direction': o.get('direction', o.get('side', '')),
    })

if not open_positions:
    print('ok')
    sys.exit()

# Find which group the new instrument belongs to
inst_groups = set()
for group_name, members in CORRELATION_GROUPS.items():
    if inst in members:
        inst_groups.add(group_name)

for pos in open_positions:
    pos_inst = pos['instrument']
    pos_dir = pos['direction']

    if pos_inst == inst:
        continue  # Same instrument check is handled by has_open_position

    # Check same correlation group
    for group_name, members in CORRELATION_GROUPS.items():
        if pos_inst in members and group_name in inst_groups:
            # Same group: block same direction (double risk)
            if pos_dir == direction:
                print(f'block_correlated:{pos_inst}')
                sys.exit()

    # Check inverse correlations
    pair = tuple(sorted([inst, pos_inst]))
    for inv_pair in INVERSE_PAIRS:
        if pair == tuple(sorted(inv_pair)):
            # Inverse pair: same direction = conflicting thesis
            if pos_dir == direction:
                print(f'block_conflict:{pos_inst}')
                sys.exit()
            break

print('ok')
" 2>/dev/null || echo "ok"
}

# Build trade submit payload using S/R levels and signal_type from scan data.
# Reads per-instrument JSON from stdin.
# Args: direction instrument conviction source reasoning price timeframe(h1|d1|m5) [strategy]
# Output: JSON payload for POST /api/v1/trades/submit
#
# Signal-type-aware entry logic:
#   trend:           breakout STOP above R1 (BUY) / below S1 (SELL), or MARKET if within 0.5 ATR
#   mean_reversion:  LIMIT at S1 (BUY) / R1 (SELL), or MARKET if within 0.5 ATR
#   mixed:           MARKET order
#
# SL/TP placed at real S/R levels with ATR buffer.
# Falls back to no SL/TP fields (bot uses ATR defaults) if S/R data missing.
build_trade_payload() {
    python3 -c "
import sys, json

d = json.load(sys.stdin)
args = sys.argv[1:]
direction, inst, conviction, source, reasoning, price_str, timeframe = args[:7]
strategy = args[7] if len(args) > 7 else None
price = float(price_str)

payload = {
    'direction': direction,
    'instrument': inst,
    'conviction': conviction,
    'source': source,
    'reasoning': reasoning,
}
if strategy:
    payload['strategy'] = strategy

levels = d.get('levels', {})
support = levels.get('support', [])
resistance = levels.get('resistance', [])
tf_data = d.get('technicals', {}).get(timeframe, {})
atr = float(tf_data.get('atr', 0) or 0)
signal_type = d.get('scoring', {}).get('signal_type', 'mixed')

if not (support and resistance and atr > 0):
    # No S/R data — plain market order, let bot use ATR defaults
    print(json.dumps(payload))
    sys.exit()

s1 = float(support[0])
s2 = float(support[1]) if len(support) > 1 else s1
r1 = float(resistance[0])
r2 = float(resistance[1]) if len(resistance) > 1 else r1
near = 0.5 * atr

if signal_type == 'trend':
    # Trend: breakout entries
    if direction == 'BUY':
        entry_level = r1           # breakout above nearest resistance
        sl_level = s1 - 0.25 * atr # below nearest support with buffer
        tp_level = r2 if r2 > r1 else r1 + 2 * atr  # next resistance or 2*ATR fallback
        distance_to_entry = abs(price - entry_level)
        if distance_to_entry <= near:
            payload['order_type'] = 'MARKET'
            ref = price
        else:
            payload['order_type'] = 'STOP'
            payload['entry_price'] = round(entry_level, 2)
            ref = entry_level
    else:  # SELL
        entry_level = s1           # breakdown below nearest support
        sl_level = r1 + 0.25 * atr
        tp_level = s2 if s2 < s1 else s1 - 2 * atr
        distance_to_entry = abs(entry_level - price)
        if distance_to_entry <= near:
            payload['order_type'] = 'MARKET'
            ref = price
        else:
            payload['order_type'] = 'STOP'
            payload['entry_price'] = round(entry_level, 2)
            ref = entry_level

elif signal_type == 'mean_reversion':
    # Mean-reversion: limit entries at S/R
    if direction == 'BUY':
        entry_level = s1           # limit at nearest support
        sl_level = s2 - 0.25 * atr if s2 < s1 else s1 - 1.5 * atr
        tp_level = r1
        distance_to_entry = abs(price - entry_level)
        if distance_to_entry <= near:
            payload['order_type'] = 'MARKET'
            ref = price
        else:
            payload['order_type'] = 'LIMIT'
            payload['entry_price'] = round(entry_level, 2)
            ref = entry_level
    else:  # SELL
        entry_level = r1           # limit at nearest resistance
        sl_level = r2 + 0.25 * atr if r2 > r1 else r1 + 1.5 * atr
        tp_level = s1
        distance_to_entry = abs(entry_level - price)
        if distance_to_entry <= near:
            payload['order_type'] = 'MARKET'
            ref = price
        else:
            payload['order_type'] = 'LIMIT'
            payload['entry_price'] = round(entry_level, 2)
            ref = entry_level

else:
    # Mixed: market order with S/R-based SL/TP
    payload['order_type'] = 'MARKET'
    ref = price
    if direction == 'BUY':
        sl_level = s1 - 0.25 * atr
        tp_level = r1
    else:
        sl_level = r1 + 0.25 * atr
        tp_level = s1

# Compute distances from reference price
if direction == 'BUY':
    sd = round(ref - sl_level, 2)
    ld = round(tp_level - ref, 2)
else:
    sd = round(sl_level - ref, 2)
    ld = round(ref - tp_level, 2)

# Per-instrument minimum stop distances (must match app/instruments.py)
# Scalp timeframe: MARKET order with M5 ATR-based SL/TP (1.0x SL, 2.0x TP)
if timeframe == 'm5':
    m5_atr = float(d.get('technicals', {}).get('m5', {}).get('atr', 0) or 0)
    if m5_atr > 0:
        sd = round(m5_atr * 1.0, 2)
        ld = round(m5_atr * 2.0, 2)
        MIN_STOP_M5 = {'BTC': 200.0, 'XAUUSD': 3.0}
        min_sd = MIN_STOP_M5.get(inst, 0)
        if sd >= min_sd:
            payload['stop_distance'] = sd
            payload['limit_distance'] = ld
    payload['order_type'] = 'MARKET'
    print(json.dumps(payload))
    sys.exit()

MIN_STOP = {'BTC': 200.0, 'XAUUSD': 5.0, 'IBUS500': 2.0, 'MES': 2.0}
min_sd = MIN_STOP.get(inst, 0)

# Only use S/R params if distances are positive, stop is wide enough, and R:R >= 1:1
if sd > 0 and ld > 0 and sd >= min_sd and ld >= sd:
    payload['stop_distance'] = sd
    payload['limit_distance'] = ld
else:
    # Distances invalid or too tight — drop, let bot use ATR defaults (with clamping)
    payload.pop('order_type', None)
    payload.pop('entry_price', None)

print(json.dumps(payload))
" "$@" 2>/dev/null || echo ""
}
