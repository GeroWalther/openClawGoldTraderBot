#!/bin/bash
# NY Opening Range Breakout scanner — runs every 5 min, 13-16 UTC (NY session window).
# Scans NZDUSD using the NY ORB scoring engine.
# Identifies the opening range from the first M15 candle after NY open (9:30 ET),
# then watches M5 bars for breakout or false-breakout entries.
# TP = 2× SL. One trade per direction per day.

source "$(dirname "$0")/common.sh"

# --- Feature flag check ---
NY_ORB_ENABLED=$(grep -E '^ny_orb_enabled=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr '[:upper:]' '[:lower:]' || true)
if [ "$NY_ORB_ENABLED" != "true" ]; then
    log "NY_ORB: Feature flag disabled (ny_orb_enabled=$NY_ORB_ENABLED), exiting"
    exit 0
fi

# Acquire lock to prevent race with other scanners
acquire_lock "trade_scanner" || exit 1

TIMESTAMP=$(date -u '+%Y-%m-%d_%H%M')
DATE=$(date -u '+%Y-%m-%d')
CSV_FILE="$JOURNAL_DIR/ny_orb/scans.csv"

mkdir -p "$JOURNAL_DIR/ny_orb/scans"

ensure_csv_header "$CSV_FILE" \
    "timestamp,instrument,price,score,direction,conviction,signal,range_high,range_low,range_size,sl_dist,tp_dist"

log "NY ORB SCAN starting"

signals_found=0

# --- Same-direction debounce (one trade per direction per day) ---
DEBOUNCE_DIR="$JOURNAL_DIR/ny_orb"
DEBOUNCE_FILE="$DEBOUNCE_DIR/last_trade.json"

check_daily_debounce() {
    local inst="$1"
    local direction="$2"
    if [ ! -f "$DEBOUNCE_FILE" ]; then
        return 0  # no previous trade — allow
    fi
    python3 -c "
import sys, json
try:
    with open('$DEBOUNCE_FILE') as f:
        d = json.load(f)
    if d.get('instrument') != '$inst':
        sys.exit(0)
    if d.get('direction') != '$direction':
        sys.exit(0)
    # Same direction on same day = skip
    if d.get('date') == '$DATE':
        print('daily_limit')
        sys.exit(1)
except Exception:
    pass
sys.exit(0)
" 2>/dev/null
    return $?
}

write_debounce() {
    local inst="$1"
    local direction="$2"
    python3 -c "
import json, time
d = {'instrument': '$inst', 'direction': '$direction', 'date': '$DATE', 'timestamp': time.time()}
with open('$DEBOUNCE_FILE', 'w') as f:
    json.dump(d, f)
" 2>/dev/null || true
}

# NY ORB instruments (only NZDUSD for now — proven in backtest)
ORB_INSTRUMENTS=("NZDUSD")

for inst in "${ORB_INSTRUMENTS[@]}"; do
    # Skip if we already have an open position for this instrument
    open=$(has_open_position "$inst")
    if [ "$open" = "yes" ]; then
        log "NY_ORB $inst: SKIP scan — already has open position"
        continue
    fi

    json=$(api_get "/api/v1/technicals/${inst}/nyorb")
    if [ -z "$json" ]; then
        log "NY_ORB: Failed to fetch $inst"
        continue
    fi

    # Save full JSON (per-scan archive + latest)
    echo "$json" > "$JOURNAL_DIR/ny_orb/scans/${DATE}_${TIMESTAMP##*_}_${inst}.json"
    echo "$json" > "$JOURNAL_DIR/ny_orb/latest_scan_${inst}.json"

    # Extract fields
    price=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('price',{}).get('current',''))" 2>/dev/null || true)
    score=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scoring',{}).get('total_score',''))" 2>/dev/null || true)
    direction=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scoring',{}).get('direction','') or '')" 2>/dev/null || true)
    conviction=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scoring',{}).get('conviction','') or '')" 2>/dev/null || true)
    signal=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scoring',{}).get('signal','') or '')" 2>/dev/null || true)
    sl_dist=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scoring',{}).get('sl_dist','0'))" 2>/dev/null || true)
    tp_dist=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scoring',{}).get('tp_dist','0'))" 2>/dev/null || true)
    range_high=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('opening_range',{}).get('range_high',''))" 2>/dev/null || true)
    range_low=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('opening_range',{}).get('range_low',''))" 2>/dev/null || true)
    range_size=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('opening_range',{}).get('range_size',''))" 2>/dev/null || true)

    # Append CSV row
    echo "${TIMESTAMP},${inst},${price},${score},${direction},${conviction},${signal},${range_high},${range_low},${range_size},${sl_dist},${tp_dist}" >> "$CSV_FILE"

    log "NY_ORB $inst: score=$score dir=$direction conv=$conviction signal=$signal range=${range_low}-${range_high}"

    # Check if we have a signal (direction set + conviction)
    if [ -n "$direction" ] && [ "$direction" != "None" ] && [ "$direction" != "null" ]; then
        if [ -n "$conviction" ] && [ "$conviction" != "None" ] && [ "$conviction" != "null" ]; then
            signals_found=$((signals_found + 1))
            log "NY_ORB $inst: SIGNAL detected ($signal, score=$score)"

            # Journal the analysis
            api_post "/api/v1/journal" "{
                \"instrument\": \"$inst\",
                \"direction\": \"${direction}\",
                \"conviction\": \"${conviction}\",
                \"total_score\": $score,
                \"factors\": {},
                \"reasoning\": \"NY ORB scan: $signal, score $score, range ${range_low}-${range_high}\",
                \"source\": \"cron_ny_orb\"
            }" > /dev/null 2>&1 || true

            # Auto-trade: only MEDIUM+HIGH conviction
            if [ "$conviction" = "MEDIUM" ] || [ "$conviction" = "HIGH" ]; then
                # Daily debounce (one trade per direction per day)
                if ! check_daily_debounce "$inst" "$direction"; then
                    log "NY_ORB $inst: SKIP trade — already traded $direction today"
                    continue
                fi

                corr_check=$(check_correlation "$inst" "$direction")
                if [ "$corr_check" != "ok" ]; then
                    corr_reason=$(echo "$corr_check" | cut -d: -f1)
                    corr_inst=$(echo "$corr_check" | cut -d: -f2)
                    log "NY_ORB $inst: SKIP trade — $corr_reason with open $corr_inst"
                else
                    # Build trade payload directly with ORB-specific SL/TP
                    # SL and TP come from the scoring engine (range-based)
                    sl_ok=$(python3 -c "print('yes' if float('${sl_dist:-0}') > 0 else 'no')" 2>/dev/null || echo "no")
                    tp_ok=$(python3 -c "print('yes' if float('${tp_dist:-0}') > 0 else 'no')" 2>/dev/null || echo "no")

                    if [ "$sl_ok" = "yes" ] && [ "$tp_ok" = "yes" ]; then
                        # Round SL/TP to 5 decimals for FX
                        sd=$(python3 -c "print(round(float('$sl_dist'), 5))" 2>/dev/null || echo "0")
                        ld=$(python3 -c "print(round(float('$tp_dist'), 5))" 2>/dev/null || echo "0")

                        payload=$(python3 -c "
import json
print(json.dumps({
    'direction': '$direction',
    'instrument': '$inst',
    'conviction': '$conviction',
    'source': 'cron_ny_orb',
    'reasoning': 'NY ORB $signal: score $score, range ${range_low}-${range_high}, SL=$sd TP=$ld',
    'order_type': 'MARKET',
    'stop_distance': float('$sd'),
    'limit_distance': float('$ld'),
    'strategy': 'ny_orb',
}))
" 2>/dev/null || echo "")

                        if [ -z "$payload" ]; then
                            log "NY_ORB $inst: SKIP trade — failed to build payload"
                        else
                            log "NY_ORB $inst: SUBMITTING MARKET trade dir=$direction conv=$conviction signal=$signal SL=$sd TP=$ld"

                            trade_result=$(api_post "/api/v1/trades/submit" "$payload" 2>&1) || true
                            if [ -n "$trade_result" ]; then
                                trade_status=$(echo "$trade_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','UNKNOWN'))" 2>/dev/null || echo "UNKNOWN")
                                trade_msg=$(echo "$trade_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('message',''))" 2>/dev/null || echo "")
                                log "NY_ORB $inst: Trade result=$trade_status — $trade_msg"
                                # Record debounce only on successful trades
                                if [ "$trade_status" != "rejected" ] && [ "$trade_status" != "UNKNOWN" ]; then
                                    write_debounce "$inst" "$direction"
                                fi
                                # Debounce on margin errors
                                if echo "$trade_msg" | grep -qi "NOT_ENOUGH_MONEY\|Insufficient balance"; then
                                    log "NY_ORB $inst: Margin insufficient — writing debounce to prevent retry"
                                    write_debounce "$inst" "$direction"
                                fi
                            else
                                log "NY_ORB $inst: Trade FAILED — no response from bot"
                            fi
                        fi
                    else
                        log "NY_ORB $inst: SKIP trade — invalid SL/TP (sl=$sl_dist tp=$tp_dist)"
                    fi
                fi
            fi
        fi
    fi
done

# Save latest scan
[ -n "${json:-}" ] && echo "$json" > "$JOURNAL_DIR/ny_orb/latest_scan.json" 2>/dev/null || true

log "NY ORB SCAN done — $signals_found signal(s) found"
