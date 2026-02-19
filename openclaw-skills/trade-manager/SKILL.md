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

This returns:
- **positions**: Open positions (instrument, direction, size, avg_cost)
- **open_orders**: Active SL/TP orders (orderId, parentId, orderType, action, price, status)
- **account**: Balance, available funds, margin
- **recent_trades**: Last 10 trades from DB with status and P&L

## Step 2: Present the Dashboard

Format the data clearly for the user:

### Open Positions
Show each position as:
```
📊 [Instrument] — [Direction] [Size] [Unit]
   Entry: [avg_cost] | Unrealized P&L: [if available]
```

### Active Orders (SL/TP)
For each open order with parentId > 0:
```
   🛑 Stop Loss: [auxPrice] (Order #[orderId])
   🎯 Take Profit: [lmtPrice] (Order #[orderId])
```

### Account
```
💰 Net Liquidation: $[value]
   Available Funds: $[value]
   Margin Used: $[value]
```

### Recent Trades
Show last 5 trades:
```
[direction] [instrument] — [status] | Entry: [price] | P&L: [pnl] | [date]
```

## Step 3: Ask What the User Wants to Do

After showing the dashboard, ask:
- **Modify SL/TP** — adjust stop-loss or take-profit on an open position
- **Close a position** — close an existing position
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

- Always show the dashboard first before any action
- Confirm with the user before modifying SL/TP or closing positions
- When modifying, show old and new values clearly
- If no open positions exist, say so clearly
- If the bot is unreachable, notify the user and do not retry
