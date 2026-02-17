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

## Submit a Trade

```bash
curl -s -X POST "$TRADING_BOT_URL/api/v1/trades/submit" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $TRADING_BOT_API_KEY" \
  -d '{
    "direction": "[BUY or SELL]",
    "stop_distance": [number in USD per ounce],
    "limit_distance": [number in USD per ounce],
    "size": [number of troy ounces, minimum 1],
    "source": "openclaw",
    "reasoning": "[analysis summary]"
  }' | jq '.'
```

## Check Positions

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

- NEVER submit without user confirmation unless confidence is HIGH and auto-trading is enabled
- Always show trade details before submitting
- Minimum size is 1 troy ounce (IBKR minimum)
- stop_distance and limit_distance are in USD per ounce (e.g., 50 means $50)
- Report the response (executed, rejected, failed)
- If bot is unreachable, notify user, do not retry
