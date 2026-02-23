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
    "instrument": "[XAUUSD, MES, IBUS500, EURUSD, EURJPY, CADJPY, USDJPY, or BTC]",
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

## Performance Analytics

```bash
# Full performance dashboard
curl -s "$TRADING_BOT_URL/api/v1/analytics?from_date=2024-01-01&to_date=2024-12-31" \
  -H "X-API-Key: $TRADING_BOT_API_KEY" | jq '.'

# Filter by instrument
curl -s "$TRADING_BOT_URL/api/v1/analytics?instrument=XAUUSD" \
  -H "X-API-Key: $TRADING_BOT_API_KEY" | jq '.'
```

Shows: win rate, avg win/loss, expectancy, profit factor, max drawdown, per-instrument breakdown, daily/weekly/monthly P&L.

## Cooldown Status

```bash
curl -s "$TRADING_BOT_URL/api/v1/analytics/cooldown" \
  -H "X-API-Key: $TRADING_BOT_API_KEY" | jq '.'
```

Shows: can_trade (bool), cooldown status, consecutive losses, daily trade count, daily P&L vs limit.

## Backtest a Strategy

```bash
curl -s -X POST "$TRADING_BOT_URL/api/v1/backtest" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $TRADING_BOT_API_KEY" \
  -d '{
    "instrument": "XAUUSD",
    "strategy": "sma_crossover",
    "period": "1y",
    "initial_balance": 10000
  }' | jq '.'
```

Available strategies: `sma_crossover`, `rsi_reversal`, `breakout`, `krabbe_scored`
Available periods: `6mo`, `1y`, `2y`, `5y`

**`krabbe_scored`** — Multi-factor strategy replicating Krabbe's 12-factor scoring system (D1 trend, 4H momentum, 1H entry, chart patterns, TF alignment, S/R proximity, TV technicals, macro fundamentals ×3 incl. yield curve, news, calendar). Covers ~70-80% of live scoring factors historically. Missing: live TradingView ratings, real-time news, community sentiment.

## Analysis Journal

Record and review AI analysis entries for forward-testing accuracy.

```bash
# List recent journal entries
curl -s "$TRADING_BOT_URL/api/v1/journal?instrument=XAUUSD" \
  -H "X-API-Key: $TRADING_BOT_API_KEY" | jq '.'

# Get journal accuracy stats
curl -s "$TRADING_BOT_URL/api/v1/journal/stats" \
  -H "X-API-Key: $TRADING_BOT_API_KEY" | jq '.'

# Update journal entry outcome after trade closes
curl -s -X PATCH "$TRADING_BOT_URL/api/v1/journal/[ID]?outcome=WIN&outcome_notes=Hit TP" \
  -H "X-API-Key: $TRADING_BOT_API_KEY" | jq '.'

# Link journal entry to executed trade
curl -s -X PATCH "$TRADING_BOT_URL/api/v1/journal/[ID]?linked_trade_id=[TRADE_ID]" \
  -H "X-API-Key: $TRADING_BOT_API_KEY" | jq '.'
```

### Journal Stats Display
```
📓 Journal Accuracy
   Total Analyses: [total_analyses]
   Outcomes Recorded: [total_with_outcome]
   Overall Win Rate: [overall_win_rate]%

   Per Conviction:
   HIGH:   [wins]/[total] = [win_rate]%
   MEDIUM: [wins]/[total] = [win_rate]%
   LOW:    [wins]/[total] = [win_rate]%

   Avg Score — Winners: [avg_score_winners] | Losers: [avg_score_losers]
```

## Rules

- ALWAYS show the FULL dashboard with ALL fields (SL, TP, P&L per position) before any action
- Never skip or summarize fields — the user needs to see entry, current price, SL, TP, and P&L for every position
- Confirm with the user before modifying SL/TP or closing positions
- When modifying, show old and new values clearly
- If no open positions exist, say so clearly
- If the bot is unreachable, notify the user and do not retry

### Performance Display (when analytics requested)
```
📈 Performance — [from_date] to [to_date]
   Win Rate: [win_rate]% ([winning]/[total] trades)
   Avg Win: $[avg_win]  |  Avg Loss: $[avg_loss]
   Expectancy: $[expectancy]/trade
   Profit Factor: [profit_factor]
   Max Drawdown: [max_drawdown]%
   Total P&L: $[total_pnl]
```
