#!/bin/bash
# Daily strategy scanner — runs once per day at 08:10 UTC.
# Strategies: XAUUSD breakout, BTC rsi_reversal, BTC sma_crossover.
# Uses daily_signals from /api/v1/technicals/{inst} response.

source "$(dirname "$0")/common.sh"

# Acquire lock — prevents race with swing/intraday scanners
acquire_lock "trade_scanner" || exit 1

TIMESTAMP=$(date -u '+%Y-%m-%d_%H%M')
DATE=$(date -u '+%Y-%m-%d')
DOW=$(date -u '+%u')  # 1=Mon ... 7=Sun
CSV_FILE="$JOURNAL_DIR/daily/scans.csv"

ensure_csv_header "$CSV_FILE" \
    "timestamp,instrument,strategy,signal,direction,conviction,price"

log "DAILY SCAN starting"

signals_found=0

# --- XAUUSD: breakout strategy ---
if [ "$DOW" -le 5 ]; then
    inst="XAUUSD"
    json=$(api_get "/api/v1/technicals/${inst}")
    if [ -z "$json" ]; then
        log "DAILY: Failed to fetch $inst"
    else
        echo "$json" > "$JOURNAL_DIR/daily/scans/${DATE}_${inst}.json"

        # Extract breakout signal
        read -r sig direction conviction price < <(
            echo "$json" | python3 -c "
import sys, json
d = json.load(sys.stdin)
ds = d.get('daily_signals', {}).get('breakout', {})
sig = 'true' if ds.get('signal') else 'false'
direction = ds.get('direction', '')
conviction = ds.get('conviction', '')
price = d.get('price', {}).get('current', '')
print(f'{sig} {direction} {conviction} {price}')
" 2>/dev/null
        )

        echo "${TIMESTAMP},${inst},breakout,${sig},${direction},${conviction},${price}" >> "$CSV_FILE"
        log "DAILY $inst breakout: signal=$sig dir=$direction conv=$conviction price=$price"

        if [ "$sig" = "true" ] && [ -n "$direction" ]; then
            signals_found=$((signals_found + 1))

            if [ "$conviction" = "MEDIUM" ] || [ "$conviction" = "HIGH" ]; then
                open=$(has_open_position "$inst")
                if [ "$open" = "yes" ]; then
                    log "DAILY $inst: SKIP — already has open position/order"
                else
                    corr_check=$(check_correlation "$inst" "$direction")
                    if [ "$corr_check" != "ok" ]; then
                        corr_reason=$(echo "$corr_check" | cut -d: -f1)
                        corr_inst=$(echo "$corr_check" | cut -d: -f2)
                        log "DAILY $inst: SKIP — $corr_reason with open $corr_inst"
                    else
                        payload=$(echo "$json" | build_trade_payload \
                            "$direction" "$inst" "$conviction" "cron_daily" \
                            "Auto daily breakout: conviction $conviction" \
                            "$price" "d1" "breakout")
                        if [ -z "$payload" ]; then
                            log "DAILY $inst: SKIP — failed to build payload"
                        else
                            log "DAILY $inst: SUBMITTING MARKET trade dir=$direction conv=$conviction strategy=breakout"
                            trade_result=$(api_post "/api/v1/trades/submit" "$payload" 2>&1) || true
                            if [ -n "$trade_result" ]; then
                                trade_status=$(echo "$trade_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','UNKNOWN'))" 2>/dev/null || echo "UNKNOWN")
                                trade_msg=$(echo "$trade_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('message',''))" 2>/dev/null || echo "")
                                log "DAILY $inst: Trade result=$trade_status — $trade_msg"
                            else
                                log "DAILY $inst: Trade FAILED — no response from bot"
                            fi
                        fi
                    fi
                fi
            fi
        fi
    fi
else
    log "DAILY XAUUSD: SKIP — weekend (day=$DOW)"
fi

# --- BTC: rsi_reversal + sma_crossover strategies ---
inst="BTC"
json=$(api_get "/api/v1/technicals/${inst}")
if [ -z "$json" ]; then
    log "DAILY: Failed to fetch $inst"
else
    echo "$json" > "$JOURNAL_DIR/daily/scans/${DATE}_${inst}.json"
    price=$(echo "$json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('price',{}).get('current',''))" 2>/dev/null || true)

    for strategy in rsi_reversal sma_crossover; do
        read -r sig direction conviction < <(
            echo "$json" | python3 -c "
import sys, json
d = json.load(sys.stdin)
ds = d.get('daily_signals', {}).get('$strategy', {})
sig = 'true' if ds.get('signal') else 'false'
direction = ds.get('direction', '')
conviction = ds.get('conviction', '')
print(f'{sig} {direction} {conviction}')
" 2>/dev/null
        )

        echo "${TIMESTAMP},${inst},${strategy},${sig},${direction},${conviction},${price}" >> "$CSV_FILE"
        log "DAILY $inst $strategy: signal=$sig dir=$direction conv=$conviction price=$price"

        if [ "$sig" = "true" ] && [ -n "$direction" ]; then
            signals_found=$((signals_found + 1))

            if [ "$conviction" = "MEDIUM" ] || [ "$conviction" = "HIGH" ]; then
                open=$(has_open_position "$inst")
                if [ "$open" = "yes" ]; then
                    log "DAILY $inst: SKIP — already has open position/order"
                    continue
                fi

                corr_check=$(check_correlation "$inst" "$direction")
                if [ "$corr_check" != "ok" ]; then
                    corr_reason=$(echo "$corr_check" | cut -d: -f1)
                    corr_inst=$(echo "$corr_check" | cut -d: -f2)
                    log "DAILY $inst: SKIP — $corr_reason with open $corr_inst"
                    continue
                fi

                payload=$(echo "$json" | build_trade_payload \
                    "$direction" "$inst" "$conviction" "cron_daily" \
                    "Auto daily $strategy: conviction $conviction" \
                    "$price" "d1" "$strategy")
                if [ -z "$payload" ]; then
                    log "DAILY $inst: SKIP — failed to build payload for $strategy"
                else
                    log "DAILY $inst: SUBMITTING MARKET trade dir=$direction conv=$conviction strategy=$strategy"
                    trade_result=$(api_post "/api/v1/trades/submit" "$payload" 2>&1) || true
                    if [ -n "$trade_result" ]; then
                        trade_status=$(echo "$trade_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','UNKNOWN'))" 2>/dev/null || echo "UNKNOWN")
                        trade_msg=$(echo "$trade_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('message',''))" 2>/dev/null || echo "")
                        log "DAILY $inst: Trade result=$trade_status — $trade_msg"
                    else
                        log "DAILY $inst: Trade FAILED — no response from bot"
                    fi
                fi
            fi
        fi
    done
fi

log "DAILY SCAN done — $signals_found signal(s) found"
