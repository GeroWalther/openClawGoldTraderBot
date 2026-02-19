---
name: gold-trader
description: Submits trade ideas to the Gold Trading Bot (IBKR) for execution
requires:
  env:
    - TRADING_BOT_URL
    - TRADING_BOT_API_KEY
  bins:
    - curl
    - jq
---

# Gold Trade Executor (IBKR)

## Open a Trade

```bash
curl -s -X POST "$TRADING_BOT_URL/api/v1/trades/submit" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $TRADING_BOT_API_KEY" \
  -d '{
    "direction": "[BUY or SELL]",
    "stop_distance": [number in USD per ounce],
    "limit_distance": [number in USD per ounce],
    "size": [number of troy ounces, minimum 1, or omit for auto-sizing],
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
    "direction": "[BUY or SELL — direction of the position to close]",
    "size": [number of troy ounces to close, or omit to close full position],
    "reasoning": "[reason for closing]"
  }' | jq '.'
```

## Check Open Positions

```bash
curl -s "$TRADING_BOT_URL/api/v1/positions" \
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
- Always show trade details before submitting
- Minimum size is 1 troy ounce (IBKR minimum), maximum is 10 troy ounces
- If size is omitted, the bot auto-calculates based on 1% account risk
- stop_distance and limit_distance are in USD per ounce (e.g., 50 means $50/oz)
- Always check open positions before opening a new trade to avoid doubling up
- When closing, specify the direction of the EXISTING position (e.g., close a BUY position by sending direction "BUY")
- Omit size when closing to close the full position
- Report the response (executed, rejected, failed, closed)
- If bot is unreachable, notify user, do not retry
