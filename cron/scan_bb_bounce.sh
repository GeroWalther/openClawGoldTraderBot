#!/bin/bash
# M15 BB Bounce scanner — runs every 15 min, 07-21 UTC, Mon-Fri.
# Scans AUDUSD using the BB Bounce scoring engine (range-specialist).
# Complements M5 scalp: trades when H1 is ranging and M5 scalp goes quiet.

source "$(dirname "$0")/common.sh"

# --- Feature flag check ---
BB_BOUNCE_ENABLED=$(grep -E '^bb_bounce_enabled=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr '[:upper:]' '[:lower:]' || true)
if [ "$BB_BOUNCE_ENABLED" != "true" ]; then
    log "M15_BB_BOUNCE: Feature flag disabled (bb_bounce_enabled=$BB_BOUNCE_ENABLED), exiting"
    exit 0
fi

# Acquire lock to prevent race with other scanners
acquire_lock "trade_scanner" || exit 1

TIMESTAMP=$(date -u '+%Y-%m-%d_%H%M')
DATE=$(date -u '+%Y-%m-%d')
CSV_FILE="$JOURNAL_DIR/bb_bounce/scans.csv"

mkdir -p "$JOURNAL_DIR/bb_bounce/scans"

ensure_csv_header "$CSV_FILE" \
    "timestamp,instrument,price,score,max_score,direction,conviction,bb_touch,rsi_extreme,bb_squeeze,session_quality"

log "M15 BB BOUNCE SCAN starting"

signals_found=0

# --- Same-direction debounce (30-minute cooldown for M15) ---
DEBOUNCE_DIR="$JOURNAL_DIR/bb_bounce"
DEBOUNCE_FILE="$DEBOUNCE_DIR/last_trade.json"
DEBOUNCE_MINUTES=30

check_debounce() {
    local inst="$1"
    local direction="$2"
    if [ ! -f "$DEBOUNCE_FILE" ]; then
        return 0
    fi
    python3 -c "
import sys, json, time
try:
    with open('$DEBOUNCE_FILE') as f:
        d = json.load(f)
    if d.get('instrument') != '$inst':
        sys.exit(0)
    if d.get('direction') != '$direction':
        sys.exit(0)
    age_min = (time.time() - d.get('timestamp', 0)) / 60
    if age_min < $DEBOUNCE_MINUTES:
        print(f'debounce:{age_min:.0f}min_ago')
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
d = {'instrument': '$inst', 'direction': '$direction', 'timestamp': time.time()}
with open('$DEBOUNCE_FILE', 'w') as f:
    json.dump(d, f)
" 2>/dev/null || true
}

# BB Bounce instruments — AUDUSD is the backtest winner
BB_BOUNCE_INSTRUMENTS=("AUDUSD")

for inst in "${BB_BOUNCE_INSTRUMENTS[@]}"; do
    # Skip if we already have an open position for this instrument
    open=$(has_open_position "$inst")
    if [ "$open" = "yes" ]; then
        log "M15_BB_BOUNCE $inst: SKIP scan — already has open position"
        continue
    fi

    json=$(api_get "/api/v1/technicals/${inst}/m15bb")
    if [ -z "$json" ]; then
        log "M15_BB_BOUNCE: Failed to fetch $inst"
        continue
    fi

    # Save full JSON
    echo "$json" > "$JOURNAL_DIR/bb_bounce/scans/${DATE}_${TIMESTAMP##*_}_${inst}.json"
    echo "$json" > "$JOURNAL_DIR/bb_bounce/latest_scan_${inst}.json"

    # Extract fields
    price=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('price',{}).get('current',''))" 2>/dev/null || true)
    score=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scoring',{}).get('total_score',''))" 2>/dev/null || true)
    max_score=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scoring',{}).get('max_score',''))" 2>/dev/null || true)
    direction=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scoring',{}).get('direction','') or '')" 2>/dev/null || true)
    conviction=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scoring',{}).get('conviction','') or '')" 2>/dev/null || true)
    bb_touch=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scoring',{}).get('factors',{}).get('bb_touch',''))" 2>/dev/null || true)
    rsi_extreme=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scoring',{}).get('factors',{}).get('rsi_extreme',''))" 2>/dev/null || true)
    bb_squeeze=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scoring',{}).get('factors',{}).get('bb_squeeze',''))" 2>/dev/null || true)
    session_quality=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scoring',{}).get('factors',{}).get('session_quality',''))" 2>/dev/null || true)

    # Append CSV row
    echo "${TIMESTAMP},${inst},${price},${score},${max_score},${direction},${conviction},${bb_touch},${rsi_extreme},${bb_squeeze},${session_quality}" >> "$CSV_FILE"

    log "M15_BB_BOUNCE $inst: score=$score/$max_score dir=$direction conv=$conviction price=$price"

    # Check signal threshold
    if [ -n "$score" ]; then
        abs_score=$(python3 -c "print(abs(float('$score')))" 2>/dev/null || echo "0")
        BB_THRESHOLD=$(grep -E '^bb_bounce_signal_threshold=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "6.0")
        is_signal=$(python3 -c "print('yes' if float('$abs_score') >= float('${BB_THRESHOLD:-6.0}') else 'no')" 2>/dev/null || echo "no")

        if [ "$is_signal" = "yes" ]; then
            signals_found=$((signals_found + 1))
            log "M15_BB_BOUNCE $inst: SIGNAL detected (score=$score)"

            # Journal the analysis
            api_post "/api/v1/journal" "{
                \"instrument\": \"$inst\",
                \"direction\": \"${direction:-NO_TRADE}\",
                \"conviction\": \"${conviction:-LOW}\",
                \"total_score\": $score,
                \"factors\": {},
                \"reasoning\": \"Automated M15 BB Bounce scan: score $score/$max_score\",
                \"source\": \"cron_m15_bb_bounce\"
            }" > /dev/null 2>&1 || true

            # Auto-trade: MEDIUM+HIGH conviction only
            if [ "$conviction" = "MEDIUM" ] || [ "$conviction" = "HIGH" ]; then
                # Same-direction debounce
                if ! check_debounce "$inst" "$direction"; then
                    debounce_info=$(check_debounce "$inst" "$direction" 2>&1 || true)
                    log "M15_BB_BOUNCE $inst: SKIP trade — same-direction debounce ($debounce_info)"
                    continue
                fi

                corr_check=$(check_correlation "$inst" "$direction")
                if [ "$corr_check" != "ok" ]; then
                    corr_reason=$(echo "$corr_check" | cut -d: -f1)
                    corr_inst=$(echo "$corr_check" | cut -d: -f2)
                    log "M15_BB_BOUNCE $inst: SKIP trade — $corr_reason with open $corr_inst"
                else
                    payload=$(echo "$json" | build_trade_payload \
                        "$direction" "$inst" "$conviction" "cron_m15_bb_bounce" \
                        "Auto M15 BB Bounce: score $score/$max_score, conviction $conviction" \
                        "$price" "m15" "m15_bb_bounce")
                    if [ -z "$payload" ]; then
                        log "M15_BB_BOUNCE $inst: SKIP trade — failed to build payload"
                    else
                        log "M15_BB_BOUNCE $inst: SUBMITTING MARKET trade dir=$direction conv=$conviction"

                        trade_result=$(api_post "/api/v1/trades/submit" "$payload" 2>&1) || true
                        if [ -n "$trade_result" ]; then
                            trade_status=$(echo "$trade_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','UNKNOWN'))" 2>/dev/null || echo "UNKNOWN")
                            trade_msg=$(echo "$trade_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('message',''))" 2>/dev/null || echo "")
                            log "M15_BB_BOUNCE $inst: Trade result=$trade_status — $trade_msg"
                            if [ "$trade_status" != "rejected" ] && [ "$trade_status" != "UNKNOWN" ]; then
                                write_debounce "$inst" "$direction"
                            fi
                            if echo "$trade_msg" | grep -qi "NOT_ENOUGH_MONEY\|Insufficient balance"; then
                                log "M15_BB_BOUNCE $inst: Margin insufficient — writing debounce to prevent retry"
                                write_debounce "$inst" "$direction"
                            fi
                        else
                            log "M15_BB_BOUNCE $inst: Trade FAILED — no response from bot"
                        fi
                    fi
                fi
            fi
        fi
    fi
done

# Save latest scan
[ -n "${json:-}" ] && echo "$json" > "$JOURNAL_DIR/bb_bounce/latest_scan.json" 2>/dev/null || true

log "M15 BB BOUNCE SCAN done — $signals_found signal(s) found"
