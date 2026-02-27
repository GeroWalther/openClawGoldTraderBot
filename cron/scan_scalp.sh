#!/bin/bash
# M5 Scalp scanner — runs every 5 min, 07-21 UTC, Mon-Fri.
# Scans XAUUSD and BTC using the 4-factor M5 scalp scoring engine.

source "$(dirname "$0")/common.sh"

# Acquire lock to prevent race with intraday/swing scanners
acquire_lock "trade_scanner" || exit 1

TIMESTAMP=$(date -u '+%Y-%m-%d_%H%M')
DATE=$(date -u '+%Y-%m-%d')
CSV_FILE="$JOURNAL_DIR/scalp/scans.csv"

ensure_csv_header "$CSV_FILE" \
    "timestamp,instrument,price,score,max_score,direction,conviction,h1_trend,m5_ema9,m5_ema21,session_quality"

log "M5 SCALP SCAN starting"

signals_found=0

# --- Same-direction debounce (defense-in-depth, matches backtest 12-bar debounce) ---
DEBOUNCE_DIR="$JOURNAL_DIR/scalp"
DEBOUNCE_FILE="$DEBOUNCE_DIR/last_trade.json"
DEBOUNCE_MINUTES=60  # skip same direction within 60 min

check_debounce() {
    local inst="$1"
    local direction="$2"
    if [ ! -f "$DEBOUNCE_FILE" ]; then
        return 0  # no previous trade — allow
    fi
    python3 -c "
import sys, json, os, time
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

# M5 scalp: BTC only (XAUUSD scalps not profitable in backtest)
SCALP_INSTRUMENTS=("BTC")

for inst in "${SCALP_INSTRUMENTS[@]}"; do
    json=$(api_get "/api/v1/technicals/${inst}/m5scalp")
    if [ -z "$json" ]; then
        log "M5_SCALP: Failed to fetch $inst"
        continue
    fi

    # Save full JSON
    echo "$json" > "$JOURNAL_DIR/scalp/scans/${DATE}_${TIMESTAMP##*_}_${inst}.json"

    # Extract fields
    price=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('price',{}).get('current',''))" 2>/dev/null)
    score=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scoring',{}).get('total_score',''))" 2>/dev/null)
    max_score=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scoring',{}).get('max_score',''))" 2>/dev/null)
    direction=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scoring',{}).get('direction','') or '')" 2>/dev/null)
    conviction=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scoring',{}).get('conviction','') or '')" 2>/dev/null)
    h1_trend=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('technicals',{}).get('h1',{}).get('trend',''))" 2>/dev/null)
    session_quality=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('scoring',{}).get('factors',{}).get('session_quality',''))" 2>/dev/null)

    # Append CSV row
    echo "${TIMESTAMP},${inst},${price},${score},${max_score},${direction},${conviction},${h1_trend},,,${session_quality}" >> "$CSV_FILE"

    log "M5_SCALP $inst: score=$score/$max_score dir=$direction conv=$conviction price=$price"

    # Check signal threshold (±5)
    if [ -n "$score" ]; then
        abs_score=$(python3 -c "print(abs(float('$score')))" 2>/dev/null || echo "0")
        is_signal=$(python3 -c "print('yes' if float('$abs_score') >= 6.0 else 'no')" 2>/dev/null || echo "no")

        if [ "$is_signal" = "yes" ]; then
            signals_found=$((signals_found + 1))
            log "M5_SCALP $inst: SIGNAL detected (score=$score)"

            # Journal the analysis
            api_post "/api/v1/journal" "{
                \"instrument\": \"$inst\",
                \"direction\": \"${direction:-NO_TRADE}\",
                \"conviction\": \"${conviction:-LOW}\",
                \"total_score\": $score,
                \"factors\": {},
                \"reasoning\": \"Automated M5 scalp scan: score $score/$max_score\",
                \"source\": \"cron_m5_scalp\"
            }" > /dev/null 2>&1 || true

            # Auto-trade: BTC only (XAUUSD scalps not profitable), MEDIUM+HIGH conviction
            if ([ "$conviction" = "MEDIUM" ] || [ "$conviction" = "HIGH" ]) && [ "$inst" = "BTC" ]; then
                open=$(has_open_position "$inst")
                if [ "$open" = "yes" ]; then
                    log "M5_SCALP $inst: SKIP trade — already has open position/order"
                    continue
                fi

                # Same-direction debounce (defense-in-depth)
                if ! check_debounce "$inst" "$direction"; then
                    debounce_info=$(check_debounce "$inst" "$direction" 2>&1 || true)
                    log "M5_SCALP $inst: SKIP trade — same-direction debounce ($debounce_info)"
                    continue
                fi

                corr_check=$(check_correlation "$inst" "$direction")
                if [ "$corr_check" != "ok" ]; then
                    corr_reason=$(echo "$corr_check" | cut -d: -f1)
                    corr_inst=$(echo "$corr_check" | cut -d: -f2)
                    log "M5_SCALP $inst: SKIP trade — $corr_reason with open $corr_inst"
                else
                    payload=$(echo "$json" | build_trade_payload \
                        "$direction" "$inst" "$conviction" "cron_m5_scalp" \
                        "Auto M5 scalp: score $score/$max_score, conviction $conviction" \
                        "$price" "m5" "m5_scalp")
                    if [ -z "$payload" ]; then
                        log "M5_SCALP $inst: SKIP trade — failed to build payload"
                    else
                        log "M5_SCALP $inst: SUBMITTING MARKET trade dir=$direction conv=$conviction"

                        trade_result=$(api_post "/api/v1/trades/submit" "$payload" 2>&1) || true
                        if [ -n "$trade_result" ]; then
                            trade_status=$(echo "$trade_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','UNKNOWN'))" 2>/dev/null || echo "UNKNOWN")
                            trade_msg=$(echo "$trade_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('message',''))" 2>/dev/null || echo "")
                            log "M5_SCALP $inst: Trade result=$trade_status — $trade_msg"
                            # Record debounce state after submission
                            write_debounce "$inst" "$direction"
                        else
                            log "M5_SCALP $inst: Trade FAILED — no response from bot"
                        fi
                    fi
                fi
            fi
        fi
    fi
done

# Save latest scan
echo "$json" > "$JOURNAL_DIR/scalp/latest_scan.json" 2>/dev/null || true

log "M5 SCALP SCAN done — $signals_found signal(s) found"
