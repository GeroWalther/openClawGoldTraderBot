---
name: market-scanner
description: Scans all supported instruments, scores each for risk/reward, and recommends the best trade opportunity
requires:
  bins:
    - curl
    - jq
---

# Market Scanner — Best Trade Finder

You are a professional multi-asset trader. When asked to scan markets or find the best trade, analyze ALL supported instruments and rank them by risk/reward quality. This gives an overview of opportunities across all markets at once.

---

## INSTRUMENTS TO SCAN

| Key | Yahoo Symbol | TradingView Symbol | Asset Class |
|-----|-------------|-------------------|-------------|
| XAUUSD | GC=F | XAUUSD | Commodity |
| MES / S&P 500 | ES=F / ^GSPC | ES1! / SPX | Equity Index |
| EURUSD | EURUSD=X | EURUSD | Forex |
| EURJPY | EURJPY=X | EURJPY | Forex |
| CADJPY | CADJPY=X | CADJPY | Forex |
| USDJPY | JPY=X | USDJPY | Forex |
| BTC | BTC-USD | BTCUSD | Crypto |

---

## PHASE 1: DATA COLLECTION

### Step 1: Fetch Current Prices (All Instruments)

```bash
for SYMBOL in "GC=F" "ES=F" "EURUSD=X" "EURJPY=X" "CADJPY=X" "JPY=X" "BTC-USD"; do
  echo "=== $SYMBOL ==="
  curl -s "https://query1.finance.yahoo.com/v8/finance/chart/$SYMBOL?range=5d&interval=1d" | jq '{
    symbol: .chart.result[0].meta.symbol,
    price: .chart.result[0].meta.regularMarketPrice,
    previousClose: .chart.result[0].meta.chartPreviousClose,
    dayChange: ((.chart.result[0].meta.regularMarketPrice - .chart.result[0].meta.chartPreviousClose) / .chart.result[0].meta.chartPreviousClose * 100),
    close_5d: .chart.result[0].indicators.quote[0].close
  }'
done
```

### Step 2: Fetch Key Macro Data

```bash
# DXY (affects gold, forex, BTC)
curl -s "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?range=5d&interval=1d" | jq '{close: .chart.result[0].indicators.quote[0].close}'

# VIX (affects S&P, risk sentiment)
curl -s "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?range=5d&interval=1d" | jq '{close: .chart.result[0].indicators.quote[0].close}'

# US 10Y Yield (affects gold, equities)
curl -s "https://query1.finance.yahoo.com/v8/finance/chart/%5ETNX?range=5d&interval=1d" | jq '{close: .chart.result[0].indicators.quote[0].close}'

# Crude Oil (affects CADJPY)
curl -s "https://query1.finance.yahoo.com/v8/finance/chart/CL=F?range=5d&interval=1d" | jq '{close: .chart.result[0].indicators.quote[0].close}'

# 13-Week T-Bill Yield (for yield curve spread = ^TNX - ^IRX)
curl -s "https://query1.finance.yahoo.com/v8/finance/chart/%5EIRX?range=5d&interval=1d" | jq '{close: .chart.result[0].indicators.quote[0].close}'
```

### Step 3: Economic Calendar

```bash
curl -s "https://nfs.faireconomy.media/ff_calendar_thisweek.json" 2>/dev/null | jq '[.[] | select((.impact == "High") and (.country == "USD" or .country == "EUR" or .country == "JPY" or .country == "ALL"))] | .[:10] | .[] | {title, country, date, time, impact}'
```

### Step 4: TradingView Quick Scan (via Tavily)

For each instrument, use **Tavily** to fetch TradingView technical ratings:

```
tavily_search: "TradingView [TRADINGVIEW_SYMBOL] technical analysis daily 4h summary rating"
```

Also extract the technicals page:
```
tavily_extract: "https://www.tradingview.com/symbols/[TRADINGVIEW_SYMBOL]/technicals/"
```

For each instrument, report:
- **TradingView Technicals rating**: 1H, 4H, Daily (Strong Buy / Buy / Neutral / Sell / Strong Sell)
- **RSI (14)** on the 4H chart
- **D1 trend direction**: SMA alignment, higher highs/lows or lower highs/lows
- Key support and resistance levels
- Any obvious chart pattern (breakout, reversal, consolidation, flag, wedge)

**Priority rule:** Daily trend > 4H setup > 1H signal. Instruments where all 3 timeframes align rank higher.

Keep it quick — you'll do a detailed analysis on the winning instrument via market-analyst.

---

## PHASE 2: SCORING EACH INSTRUMENT

For each instrument, assign quick scores (-2 to +2):

| Factor | Description |
|--------|-------------|
| Trend | Daily SMA alignment: strong trend = +/-2, choppy = 0 |
| Momentum | 4H RSI + MACD: strong momentum = +/-2, divergence = reduce |
| S/R Quality | Clear nearby S/R for stop placement = +2, no clear level = 0 |
| TV Rating | TradingView technicals summary rating |
| Fundamental | Key macro factor for this instrument |
| Calendar | Major event risk: none = +2, event within 4h = -2 |

**Score range per instrument: -12 to +12**

### Filter Rules (DISQUALIFY if any apply)
- RSI > 75 and signal would be BUY → skip
- RSI < 25 and signal would be SELL → skip
- Major economic event within 4 hours affecting this instrument → skip
- Market closed → skip
- Outside active trading session → skip (see session hours below)
- Score between -4 and +4 → insufficient edge, skip
- No clear S/R level for stop placement → skip

### Active Trading Sessions (UTC)
| Instrument | Sessions | Hours (UTC) |
|---|---|---|
| XAUUSD | London / New York | 07-16 / 13-21 |
| MES / S&P 500 | US Market | 13-20 |
| EURUSD | London+NY | 07-21 |
| EURJPY | Tokyo / London | 00-09 / 07-16 |
| CADJPY | Tokyo / London+NY | 00-09 / 07-21 |
| USDJPY | Tokyo / London+NY | 00-09 / 07-21 |
| BTC | 24/7 | All hours (low liquidity weekends) |

### Risk/Reward Assessment

For each qualifying instrument:
1. Identify the nearest S/R level for stop placement
2. Calculate stop distance (entry → S/R + buffer)
3. Identify the next S/R target for take-profit
4. Calculate R:R ratio = target distance / stop distance
5. **Only include if R:R >= 1.5:1**

---

## PHASE 3: RANKING AND RECOMMENDATION

Rank qualifying instruments by a composite score:

**Composite = (Direction Score / 12) × 40% + (R:R Ratio / 4) × 40% + (Trend Clarity / 2) × 20%**

Where:
- Direction Score: absolute value of the instrument's total score
- R:R Ratio: capped at 4:1 for scoring purposes
- Trend Clarity: 0 = choppy, 1 = mild trend, 2 = strong clean trend

---

## PHASE 4: OUTPUT FORMAT

```
MARKET SCAN — [DATE] [TIME] UTC

━━━ MACRO CONTEXT ━━━
DXY: [value] [↑/↓] | VIX: [value] [↑/↓] | US 10Y: [value] [↑/↓] | Crude: [value] [↑/↓] | Yield Curve: [spread] [steepening/flattening]
Upcoming Events: [next high-impact event or "none within 4h"]
Overall Regime: [Risk-On / Risk-Off / Mixed]

━━━ INSTRUMENT SCORES ━━━

| Instrument | Price | Day% | Trend | Mom | S/R | TV | Fund | Cal | TOTAL | Signal |
|------------|-------|------|-------|-----|-----|-----|------|-----|-------|--------|
| XAUUSD     | $X    | X%   | X     | X   | X   | X   | X    | X   | X/12  | BUY/SELL/- |
| S&P 500    | $X    | X%   | X     | X   | X   | X   | X    | X   | X/12  | BUY/SELL/- |
| EUR/USD    | X     | X%   | X     | X   | X   | X   | X    | X   | X/12  | BUY/SELL/- |
| EUR/JPY    | X     | X%   | X     | X   | X   | X   | X    | X   | X/12  | BUY/SELL/- |
| CAD/JPY    | X     | X%   | X     | X   | X   | X   | X    | X   | X/12  | BUY/SELL/- |
| USD/JPY    | X     | X%   | X     | X   | X   | X   | X    | X   | X/12  | BUY/SELL/- |
| BTC        | $X    | X%   | X     | X   | X   | X   | X    | X   | X/12  | BUY/SELL/- |

Disqualified: [list instruments that were filtered out and why]

━━━ TOP TRADE OPPORTUNITY ━━━

[If no qualifying instruments:]
NO TRADE — [reason, e.g. "all instruments lack edge" or "high-impact event imminent"]
Next scan: [suggested time]

[If qualifying instrument(s) found:]
#1 BEST TRADE:
Instrument: [KEY]
Direction: [BUY/SELL]
Score: [X/12]
Confidence: [LOW/MEDIUM/HIGH]
Conviction: [HIGH / MEDIUM / LOW]
Entry: Around [price]
Stop Distance: [number] (fixed bracket, beyond [S/R level at $X])
Limit Distance: [number] (target [S/R level at $X])
R:R Ratio: [X:1]
Risk: 1% of account

Reasoning: [2-3 sentences — why this is the best opportunity right now]

[If there's a clear #2:]
#2 RUNNER-UP:
Instrument: [KEY]
Direction: [BUY/SELL]
Score: [X/12]
R:R Ratio: [X:1]
Brief: [1 sentence — why it's second choice]
```

---

## IMPORTANT RULES

1. **Scan ALL instruments** — don't skip any unless the market is closed
2. **Never force a trade.** If nothing qualifies, say "no trade" — that IS the recommendation
3. **R:R is king.** A high score with poor R:R loses to a moderate score with great R:R
4. **Data first.** Collect all data before scoring. Never guess missing data.
5. **Be specific.** Exact prices, exact levels, exact distances
6. **If asked to execute the trade**, use the market-trader skill with the recommended instrument and parameters
7. **Speed over depth.** This is a quick scan, not a deep analysis. If the user wants deeper analysis on the winner, they should use market-analyst
8. **Calendar is a hard filter.** If NFP/CPI/Fed is within 4 hours, that instrument is disqualified regardless of score
