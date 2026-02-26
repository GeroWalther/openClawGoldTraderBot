#!/bin/bash
# Swing scanner — runs 3x/day: 08:00, 13:00, 19:00 UTC (09, 14, 20 Berlin).
# Scans all instruments using the 12-factor swing scoring engine.

source "$(dirname "$0")/common.sh"

# Acquire lock — prevents race with intraday scanner
acquire_lock "trade_scanner" || exit 1

TIMESTAMP=$(date -u '+%Y-%m-%d_%H%M')
DATE=$(date -u '+%Y-%m-%d')
CSV_FILE="$JOURNAL_DIR/swing/scans.csv"

ensure_csv_header "$CSV_FILE" \
    "timestamp,instrument,price,change_pct,d1_trend,d1_rsi,score,max_score,direction,conviction,calendar_score,news_score"

log "SWING SCAN starting"

# Fetch all instruments at once
json=$(api_get "/api/v1/technicals/scan")
if [ -z "$json" ]; then
    log "SWING: Failed to fetch /technicals/scan"
    exit 1
fi

# Save full JSON
echo "$json" > "$JOURNAL_DIR/swing/scans/${TIMESTAMP}.json"
echo "$json" > "$JOURNAL_DIR/swing/latest_scan.json"

# Parse each instrument
instrument_count=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('instruments',[])))" 2>/dev/null || echo "0")

signals_found=0

for i in $(seq 0 $((instrument_count - 1))); do
    read -r inst price change_pct d1_trend d1_rsi score max_score direction conviction cal_score news_score < <(
        echo "$json" | python3 -c "
import sys, json
d = json.load(sys.stdin)
items = d.get('instruments', [])
if $i < len(items):
    item = items[$i]
    inst = item.get('instrument', '')
    price = item.get('price', {}).get('current', '')
    change = item.get('price', {}).get('change_pct', '')
    d1 = item.get('technicals', {}).get('d1', {})
    d1_trend = d1.get('trend', '')
    d1_rsi = d1.get('rsi', '')
    sc = item.get('scoring', {})
    score = sc.get('total_score', '')
    max_sc = sc.get('max_score', '')
    direction = sc.get('direction', '') or ''
    conviction = sc.get('conviction', '') or ''
    cal = item.get('calendar', {}).get('score', '')
    news = item.get('news', {}).get('score', '')
    print(f'{inst} {price} {change} {d1_trend} {d1_rsi} {score} {max_sc} {direction} {conviction} {cal} {news}')
" 2>/dev/null
    )

    if [ -z "$inst" ]; then
        continue
    fi

    # Append CSV row
    echo "${TIMESTAMP},${inst},${price},${change_pct},${d1_trend},${d1_rsi},${score},${max_score},${direction},${conviction},${cal_score},${news_score}" >> "$CSV_FILE"

    log "SWING $inst: score=$score/$max_score dir=$direction conv=$conviction price=$price"

    # Check signal threshold (±7)
    if [ -n "$score" ]; then
        abs_score=$(python3 -c "print(abs(float('$score')))" 2>/dev/null || echo "0")
        is_signal=$(python3 -c "print('yes' if float('$abs_score') >= 7.0 else 'no')" 2>/dev/null || echo "no")

        if [ "$is_signal" = "yes" ]; then
            signals_found=$((signals_found + 1))
            log "SWING $inst: SIGNAL detected (score=$score)"

            # Journal the analysis
            api_post "/api/v1/journal" "{
                \"instrument\": \"$inst\",
                \"direction\": \"${direction:-NO_TRADE}\",
                \"conviction\": \"${conviction:-LOW}\",
                \"total_score\": $score,
                \"factors\": {},
                \"reasoning\": \"Automated swing scan: score $score/$max_score\",
                \"source\": \"cron_swing\"
            }" > /dev/null 2>&1 || true

            # Auto-trade: MEDIUM+HIGH conviction, only tradeable instruments
            if ([ "$conviction" = "MEDIUM" ] || [ "$conviction" = "HIGH" ]) && ([ "$inst" = "XAUUSD" ] || [ "$inst" = "BTC" ]); then
                open=$(has_open_position "$inst")
                if [ "$open" = "yes" ]; then
                    log "SWING $inst: SKIP trade — already has open position/order"
                    continue
                fi

                corr_check=$(check_correlation "$inst" "$direction")
                if [ "$corr_check" != "ok" ]; then
                    corr_reason=$(echo "$corr_check" | cut -d: -f1)
                    corr_inst=$(echo "$corr_check" | cut -d: -f2)
                    log "SWING $inst: SKIP trade — $corr_reason with open $corr_inst"
                else
                    # Extract per-instrument JSON for S/R level computation
                    inst_json=$(echo "$json" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin).get('instruments',[])[$i]))" 2>/dev/null)
                    payload=$(echo "$inst_json" | build_trade_payload \
                        "$direction" "$inst" "$conviction" "cron_swing" \
                        "Auto swing: score $score/$max_score, conviction $conviction" \
                        "$price" "d1" "swing")
                    if [ -z "$payload" ]; then
                        log "SWING $inst: SKIP trade — failed to build payload"
                    else
                        order_type=$(echo "$payload" | python3 -c "import sys,json; print(json.load(sys.stdin).get('order_type','MARKET'))" 2>/dev/null || echo "MARKET")
                        log "SWING $inst: SUBMITTING $order_type trade dir=$direction conv=$conviction"

                        trade_result=$(api_post "/api/v1/trades/submit" "$payload" 2>&1) || true
                        if [ -n "$trade_result" ]; then
                            trade_status=$(echo "$trade_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','UNKNOWN'))" 2>/dev/null || echo "UNKNOWN")
                            trade_msg=$(echo "$trade_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('message',''))" 2>/dev/null || echo "")
                            log "SWING $inst: Trade result=$trade_status — $trade_msg"
                        else
                            log "SWING $inst: Trade FAILED — no response from bot"
                        fi
                    fi
                fi
            fi
        fi
    fi
done

log "SWING SCAN done — $signals_found signal(s) found"
