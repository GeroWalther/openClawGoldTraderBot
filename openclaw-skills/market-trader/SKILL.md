---
name: market-trader
description: Submits trade ideas to the Trading Bot (IBKR) for execution — supports multiple instruments
requires:
  env:
    - TRADING_BOT_URL
    - TRADING_BOT_API_KEY
  bins:
    - curl
    - jq
---

# Trade Executor (IBKR)

## Supported Instruments

| Key | Name | Min Size | Size Unit | Stop Distance Unit |
|-----|------|----------|-----------|-------------------|
| XAUUSD | Gold Spot | 1 | oz | USD/oz (e.g. 50 = $50) |
| MES | Micro E-mini S&P 500 | 1 | contracts | Points (e.g. 20 = 20 pts) |
| IBUS500 | S&P 500 CFD | 1 | units | Points (e.g. 20 = 20 pts) |
| EURUSD | EUR/USD | 20000 | units | Pips as decimal (e.g. 0.0050 = 50 pips) |
| EURJPY | EUR/JPY | 20000 | units | Pips as decimal (e.g. 0.50 = 50 pips) |
| BTC | Micro Bitcoin Futures | 1 | contracts | USD (e.g. 2000 = $2,000) |

## Open a Trade

```bash
curl -s -X POST "$TRADING_BOT_URL/api/v1/trades/submit" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $TRADING_BOT_API_KEY" \
  -d '{
    "instrument": "[XAUUSD, MES, IBUS500, EURUSD, EURJPY, or BTC]",
    "direction": "[BUY or SELL]",
    "stop_distance": [number — see table above for units],
    "limit_distance": [number — same unit as stop_distance],
    "size": [position size or omit for auto-sizing],
    "source": "openclaw",
    "reasoning": "[analysis summary]"
  }' | jq '.'
```

## Close a Position

```bash
curl -s -X POST "$TRADING_BOT_URL/api/v1/positions/close" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $TRADING_BOT_API_KEY" \
  -d '{
    "instrument": "[XAUUSD, MES, IBUS500, EURUSD, EURJPY, or BTC]",
    "direction": "[BUY or SELL — direction of the position to close]",
    "size": [size to close, or omit to close full position],
    "reasoning": "[reason for closing]"
  }' | jq '.'
```

## Check Open Positions

```bash
# All positions
curl -s "$TRADING_BOT_URL/api/v1/positions" \
  -H "X-API-Key: $TRADING_BOT_API_KEY" | jq '.'

# Filtered by instrument
curl -s "$TRADING_BOT_URL/api/v1/positions?instrument=XAUUSD" \
  -H "X-API-Key: $TRADING_BOT_API_KEY" | jq '.'
```

## Check Account Balance

```bash
curl -s "$TRADING_BOT_URL/api/v1/positions/account" \
  -H "X-API-Key: $TRADING_BOT_API_KEY" | jq '.'
```

## Check Bot Health

```bash
curl -s "$TRADING_BOT_URL/health" | jq '.'
```

## Rules

- NEVER open a trade without user confirmation unless confidence is HIGH and auto-trading is enabled
- Always include the `instrument` field — omitting it defaults to XAUUSD
- Always show trade details before submitting
- If size is omitted, the bot auto-calculates based on 1% account risk
- stop_distance and limit_distance units depend on the instrument (see table)
- Always check open positions before opening a new trade to avoid doubling up
- When closing, specify the direction of the EXISTING position (e.g., close a BUY position by sending direction "BUY")
- Omit size when closing to close the full position
- Report the response (executed, rejected, failed, closed)
- If bot is unreachable, notify user, do not retry
