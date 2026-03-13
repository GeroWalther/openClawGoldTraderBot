#!/bin/bash
# M15 Sensei scanner — runs every 15 min, 24/7 (BTC trades all week).
# Scans BTCUSD using the Sensei (W/M pattern) scoring engine.

source "$(dirname "$0")/common.sh"

# --- Feature flag check ---
SENSEI_ENABLED=$(grep -E '^sensei_btc_enabled=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr '[:upper:]' '[:lower:]' || true)
if [ "$SENSEI_ENABLED" != "true" ]; then
    log "M15_SENSEI: Feature flag disabled (sensei_btc_enabled=$SENSEI_ENABLED), exiting"
    exit 0
fi

# Acquire lock to prevent race with other scanners
acquire_lock "trade_scanner" || exit 1

TIMESTAMP=$(date -u '+%Y-%m-%d_%H%M')
DATE=$(date -u '+%Y-%m-%d')
CSV_FILE="$JOURNAL_DIR/sensei/scans.csv"

mkdir -p "$JOURNAL_DIR/sensei/scans"

ensure_csv_header "$CSV_FILE" \
    "timestamp,instrument,price,score,max_score,direction,conviction,consolidation,pattern,sma20_cross,trend,rsi"

log "M15 SENSEI SCAN starting"

signals_found=0

# --- Same-direction debounce (60-minute cooldown) ---
DEBOUNCE_DIR="$JOURNAL_DIR/sensei"
DEBOUNCE_FILE="$DEBOUNCE_DIR/last_trade.json"
DEBOUNCE_MINUTES=60

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

# M15 Sensei: BTCUSD only
SENSEI_INSTRUMENTS=("BTC")

for inst in "${SENSEI_INSTRUMENTS[@]}"; do
    json=$(api_get "/api/v1/technicals/${inst}/m15sensei")
    if [ -z "$json" ]; then
        log "M15_SENSEI: Failed to fetch $inst"
        continue
    fi

    # Save full JSON
    echo "$json" > "$JOURNAL_DIR/sensei/scans/${DATE}_${TIMESTAMP##*_}_${inst}.json"

    # Extract fields (|| true prevents pipefail from killing the script)
    price=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('price',{}).get('current',''))" 2>/dev/null || true)
    score=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scoring',{}).get('total_score',''))" 2>/dev/null || true)
    max_score=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scoring',{}).get('max_score',''))" 2>/dev/null || true)
    direction=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scoring',{}).get('direction','') or '')" 2>/dev/null || true)
    conviction=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scoring',{}).get('conviction','') or '')" 2>/dev/null || true)
    consol=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scoring',{}).get('factors',{}).get('consolidation_quality',''))" 2>/dev/null || true)
    pattern=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scoring',{}).get('factors',{}).get('pattern_quality',''))" 2>/dev/null || true)
    sma20_cross=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scoring',{}).get('factors',{}).get('sma20_cross',''))" 2>/dev/null || true)
    trend=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scoring',{}).get('factors',{}).get('trend_alignment',''))" 2>/dev/null || true)
    rsi=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scoring',{}).get('factors',{}).get('rsi_confirmation',''))" 2>/dev/null || true)

    # Append CSV row
    echo "${TIMESTAMP},${inst},${price},${score},${max_score},${direction},${conviction},${consol},${pattern},${sma20_cross},${trend},${rsi}" >> "$CSV_FILE"

    log "M15_SENSEI $inst: score=$score/$max_score dir=$direction conv=$conviction price=$price"

    # Check signal threshold
    if [ -n "$score" ]; then
        abs_score=$(python3 -c "print(abs(float('$score')))" 2>/dev/null || echo "0")
        is_signal=$(python3 -c "print('yes' if float('$abs_score') >= 8.0 else 'no')" 2>/dev/null || echo "no")

        if [ "$is_signal" = "yes" ]; then
            signals_found=$((signals_found + 1))
            log "M15_SENSEI $inst: SIGNAL detected (score=$score)"

            # Journal the analysis
            api_post "/api/v1/journal" "{
                \"instrument\": \"$inst\",
                \"direction\": \"${direction:-NO_TRADE}\",
                \"conviction\": \"${conviction:-LOW}\",
                \"total_score\": $score,
                \"factors\": {},
                \"reasoning\": \"Automated M15 Sensei scan: score $score/$max_score\",
                \"source\": \"cron_m15_sensei\"
            }" > /dev/null 2>&1 || true

            # Auto-trade: BTC M15 Sensei, MEDIUM+HIGH conviction
            if ([ "$conviction" = "MEDIUM" ] || [ "$conviction" = "HIGH" ]) && [ "$inst" = "BTC" ]; then
                open=$(has_open_position "$inst")
                if [ "$open" = "yes" ]; then
                    log "M15_SENSEI $inst: SKIP trade — already has open position/order"
                    continue
                fi

                # Same-direction debounce
                if ! check_debounce "$inst" "$direction"; then
                    debounce_info=$(check_debounce "$inst" "$direction" 2>&1 || true)
                    log "M15_SENSEI $inst: SKIP trade — same-direction debounce ($debounce_info)"
                    continue
                fi

                corr_check=$(check_correlation "$inst" "$direction")
                if [ "$corr_check" != "ok" ]; then
                    corr_reason=$(echo "$corr_check" | cut -d: -f1)
                    corr_inst=$(echo "$corr_check" | cut -d: -f2)
                    log "M15_SENSEI $inst: SKIP trade — $corr_reason with open $corr_inst"
                else
                    payload=$(echo "$json" | build_trade_payload \
                        "$direction" "$inst" "$conviction" "cron_m15_sensei" \
                        "Auto M15 Sensei: score $score/$max_score, conviction $conviction" \
                        "$price" "m15" "m15_sensei")
                    if [ -z "$payload" ]; then
                        log "M15_SENSEI $inst: SKIP trade — failed to build payload"
                    else
                        log "M15_SENSEI $inst: SUBMITTING MARKET trade dir=$direction conv=$conviction"

                        trade_result=$(api_post "/api/v1/trades/submit" "$payload" 2>&1) || true
                        if [ -n "$trade_result" ]; then
                            trade_status=$(echo "$trade_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','UNKNOWN'))" 2>/dev/null || echo "UNKNOWN")
                            trade_msg=$(echo "$trade_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('message',''))" 2>/dev/null || echo "")
                            log "M15_SENSEI $inst: Trade result=$trade_status — $trade_msg"
                            if [ "$trade_status" != "rejected" ] && [ "$trade_status" != "UNKNOWN" ]; then
                                write_debounce "$inst" "$direction"
                            fi
                        else
                            log "M15_SENSEI $inst: Trade FAILED — no response from bot"
                        fi
                    fi
                fi
            fi
        fi
    fi
done

# Save latest scan
echo "$json" > "$JOURNAL_DIR/sensei/latest_scan.json" 2>/dev/null || true

log "M15 SENSEI SCAN done — $signals_found signal(s) found"
