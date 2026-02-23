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
| CADJPY | CAD/JPY | 20000 | units | Pips as decimal (e.g. 0.50 = 50 pips) |
| USDJPY | USD/JPY | 20000 | units | Pips as decimal (e.g. 0.50 = 50 pips) |
| BTC | Micro Bitcoin Futures | 1 | contracts | USD (e.g. 2000 = $2,000) |

## Open a Trade

```bash
curl -s -X POST "$TRADING_BOT_URL/api/v1/trades/submit" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $TRADING_BOT_API_KEY" \
  -d '{
    "instrument": "[XAUUSD, MES, IBUS500, EURUSD, EURJPY, CADJPY, USDJPY, or BTC]",
    "direction": "[BUY or SELL]",
    "stop_distance": [number — see table above for units],
    "limit_distance": [number — same unit as stop_distance],
    "size": [position size or omit for auto-sizing],
    "source": "openclaw",
    "reasoning": "[analysis summary]",
    "conviction": "[HIGH, MEDIUM, or LOW — from analysis score]",
    "order_type": "[MARKET, LIMIT, or STOP — default MARKET]",
    "entry_price": [required for LIMIT/STOP orders — price to enter at]
  }' | jq '.'
```

### Order Types

| Type | Description | entry_price Rule |
|------|-------------|-----------------|
| MARKET | Execute immediately at current price (default) | Not needed |
| LIMIT | Enter on pullback/reversal | BUY: below current price, SELL: above current price |
| STOP | Enter on breakout | BUY: above current price, SELL: below current price |

- `order_type` defaults to `"MARKET"` if omitted (backward compatible)
- `entry_price` is **required** for LIMIT and STOP orders
- SL/TP distances are calculated from `entry_price` (not current price) for pending orders
- Pending orders use GTC (Good Till Cancel) — they stay active until filled or cancelled

## Cancel a Pending Order

```bash
curl -s -X POST "$TRADING_BOT_URL/api/v1/trades/cancel" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $TRADING_BOT_API_KEY" \
  -d '{
    "instrument": "[XAUUSD, MES, IBUS500, EURUSD, EURJPY, CADJPY, USDJPY, or BTC]",
    "direction": "[BUY or SELL]",
    "order_id": [specific order ID, or omit to cancel by instrument+direction]
  }' | jq '.'
```

- Cancels the parent order; IBKR auto-cancels SL/TP children
- Use `order_id` to cancel a specific order, or omit to cancel all matching pending orders

## Close a Position

```bash
curl -s -X POST "$TRADING_BOT_URL/api/v1/positions/close" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $TRADING_BOT_API_KEY" \
  -d '{
    "instrument": "[XAUUSD, MES, IBUS500, EURUSD, EURJPY, CADJPY, USDJPY, or BTC]",
    "direction": "[BUY or SELL — direction of the position to close]",
    "size": [size to close, or omit to close full position],
    "reasoning": "[reason for closing]"
  }' | jq '.'
```

## Modify SL/TP

```bash
curl -s -X POST "$TRADING_BOT_URL/api/v1/positions/modify" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $TRADING_BOT_API_KEY" \
  -d '{
    "instrument": "[XAUUSD, MES, IBUS500, EURUSD, EURJPY, CADJPY, USDJPY, or BTC]",
    "direction": "[BUY or SELL — direction of the position]",
    "new_stop_loss": [new SL price or omit to keep current],
    "new_take_profit": [new TP price or omit to keep current],
    "reasoning": "[reason for modification]"
  }' | jq '.'
```

- Specify at least one of `new_stop_loss` or `new_take_profit`
- Direction is the direction of the EXISTING position (same as close)
- The bot finds the matching SL/TP orders automatically via IBKR open trades

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

## ATR Auto-Defaults

If `stop_distance` and `limit_distance` are omitted, the bot calculates ATR-based stops automatically:
- SL = ATR(14) × 1.5
- TP = ATR(14) × 2.0
- Clamped to instrument min/max bounds

You can override by specifying explicit distances.

## Partial Take-Profit

When enabled (default), the bot splits the TP into two orders:
- **TP1**: 50% of position at 1R (1× risk distance)
- **TP2**: Remaining 50% at full TP target
- Falls back to single TP if position size is too small to split

## Session & Cooldown Enforcement

The bot automatically enforces:
- **Session filter**: Rejects trades outside active trading hours
- **Cooldown**: After 2 consecutive losses, trading paused for 2h (3 losses → 4h)
- **Daily limits**: Max 5 trades/day, max 3% daily loss
