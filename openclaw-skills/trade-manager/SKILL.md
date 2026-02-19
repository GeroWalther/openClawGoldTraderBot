---
name: trade-manager
description: Trade status dashboard — shows positions, orders, account, history. Allows modifying SL/TP and closing positions.
requires:
  env:
    - TRADING_BOT_URL
    - TRADING_BOT_API_KEY
  bins:
    - curl
    - jq
---

# Trade Manager Dashboard

## Step 1: Get Full Trade Status

```bash
curl -s "$TRADING_BOT_URL/api/v1/positions/status" \
  -H "X-API-Key: $TRADING_BOT_API_KEY" | jq '.'
```

The response contains: `positions`, `open_orders`, `account`, `recent_trades`.

Each position already includes: `instrument`, `direction`, `size`, `size_unit`, `avg_cost`, `current_price`, `unrealized_pnl`, `stop_loss`, `take_profit`.

## Step 2: Present the Dashboard

**IMPORTANT: You MUST show ALL of the following fields for each position. Do not simplify or skip fields.**

### Open Positions

For EACH position in `positions`, show ALL of these fields:

```
📊 [instrument] — [direction] [size] [size_unit]
   Entry: $[avg_cost]  |  Current: $[current_price]
   🛑 Stop Loss: $[stop_loss]  |  🎯 Take Profit: $[take_profit]
   P&L: [unrealized_pnl] (show + or - with $ sign, green/red emoji)
```

Example:
```
📊 XAUUSD — SELL 10 oz
   Entry: $4,981.23  |  Current: $4,986.58
   🛑 SL: $5,081.92  |  🎯 TP: $4,831.92
   P&L: -$53.42 📉
```

### Account Summary

```
💰 Balance: $[NetLiquidation]
   Available: $[AvailableFunds]  |  Margin: $[MaintMarginReq]
```

### Recent Trades (last 5 with status "executed" or "closed")

```
[direction] [epic] — [status] | Entry: $[entry_price] | SL: $[stop_loss] | TP: $[take_profit] | P&L: $[pnl] | [created_at date]
```

Skip trades with status "failed" unless they are the only ones.

## Step 3: Ask What the User Wants to Do

After showing the full dashboard, ask:
- **Modify SL/TP** — adjust stop-loss or take-profit
- **Close a position** — close an open position
- **Refresh** — get updated status

## Modify SL/TP

```bash
curl -s -X POST "$TRADING_BOT_URL/api/v1/positions/modify" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $TRADING_BOT_API_KEY" \
  -d '{
    "instrument": "[XAUUSD, MES, IBUS500, EURUSD, EURJPY, or BTC]",
    "direction": "[BUY or SELL]",
    "new_stop_loss": [new SL price or omit],
    "new_take_profit": [new TP price or omit],
    "reasoning": "[reason]"
  }' | jq '.'
```

## Close a Position

```bash
curl -s -X POST "$TRADING_BOT_URL/api/v1/positions/close" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $TRADING_BOT_API_KEY" \
  -d '{
    "instrument": "[instrument]",
    "direction": "[BUY or SELL]",
    "reasoning": "[reason]"
  }' | jq '.'
```

## Rules

- ALWAYS show the FULL dashboard with ALL fields (SL, TP, P&L per position) before any action
- Never skip or summarize fields — the user needs to see entry, current price, SL, TP, and P&L for every position
- Confirm with the user before modifying SL/TP or closing positions
- When modifying, show old and new values clearly
- If no open positions exist, say so clearly
- If the bot is unreachable, notify the user and do not retry
