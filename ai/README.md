# Krabbe v2 — AI Discretionary Trading System

AI-driven trading layer for gold-trader. Replaces fixed-strategy scanners with Claude-powered chart analysis via TradingView MCP.

## Architecture

```
┌──────────────────────────────── MAC HOST ────────────────────────────────┐
│                                                                          │
│  TradingView Desktop   ◀──────┐       macOS cron                         │
│  (--remote-debugging-          │   ┌───────────────────────────────┐     │
│   port=9222)                   │   │ */30  ai_regime.sh            │     │
│         ▲                      │   │ */15  ai_scan.sh ─┐           │     │
│         │ CDP                  │   │ */15  ai_manage.sh │           │     │
│         │                      │   │ 21:05 ai_review.sh │           │     │
│  TradingView MCP  ◀───┐        │   │ Fri  ai_eod.sh    │           │     │
│  (Node.js)            │        │   └────────────────────┼───────────┘     │
│         ▲             │        │                         │                │
│         │  stdio      │        │    ┌────────────────────┘                │
│         │             │        │    ▼                                     │
│  Claude Code CLI  ────┘        │  ai_trade.sh (on-demand from ai_scan)   │
│  (Claude Max sub)              │                                          │
│         │                      │                                          │
│         └─── reads regime ─────┘                                         │
│         └─── calls API ──────────────────────┐                           │
│                                              │                           │
└──────────────────────────────────────────────┼──────────────────────────-┘
                                               │ HTTP (x-api-key)
                                               ▼
┌──────────────────────── DOCKER (localhost:8001) ─────────────────────────┐
│                                                                          │
│  gold-trader FastAPI                                                     │
│  ├── /api/v1/trades/submit        ── AI submits trades here              │
│  ├── /api/v1/positions/modify     ── AI trails SL here                   │
│  ├── /api/v1/positions/close      ── AI closes positions here            │
│  ├── /api/v1/positions/status     ── AI reads positions                  │
│  └── /api/v1/journal              ── AI logs decisions                   │
│                                                                          │
│  TradeExecutor ──► RiskManager ──► PositionSizer ──► Broker              │
│  (Layer 0 safety — AI cannot override these limits)                      │
│                                                                          │
│  In-container cron (AI mode):                                            │
│  - monitor.sh (trade monitor, backup ratchet)                            │
│  - eod_close.sh (Friday backup)                                          │
│  - daily_summary.sh (performance report)                                 │
└──────────────────────────────────────────────────────────────────────────┘
```

## Layered Design

- **Layer 0 — Hard Risk Limits** (Python code, AI cannot override)
  - Max X% risk per trade (from `conviction_*_risk_pct`)
  - Max daily/weekly drawdown → kill switch
  - Cooldown after consecutive losses
  - Spread protection, session filter, correlation checks

- **Layer 1 — Regime Detector** (`ai_regime.sh`, every 30min)
  - Claude reads D1/H4/H1 via TradingView MCP
  - Classifies: trending_up, trending_down, ranging, volatile_choppy, low_vol_squeeze
  - Outputs `ai/state/regime.json`

- **Layer 2 — Setup Scanner** (`ai_scan.sh`, every 15min)
  - Only runs on instruments flagged `instruments_in_play` in regime
  - Looks for setups matching the regime's playbook
  - Can inject Pine Script from `ai/pine/` to verify patterns quantitatively

- **Layer 3 — Trade Decision** (`ai_trade.sh`, on-demand from scanner)
  - Re-verifies setup is still valid NOW
  - Checks open positions, account, recent journal, insights
  - Decides: BUY / SELL / PASS (most outputs should be PASS)
  - Uses Opus for this critical decision

- **Layer 4 — Execution** — Gold-trader's existing `TradeExecutor` pipeline
  - Same validation/risk/sizing as classic mode — no shortcuts

- **Layer 5 — Position Management** (`ai_manage.sh`, every 15min)
  - Structural SL trailing (not fixed percentages)
  - Partial closes, move to breakeven, full close on thesis break

- **Layer 6 — Daily Review** (`ai_review.sh`, 21:05 UTC)
  - Reviews all trades, extracts lessons
  - Updates `ai/state/insights.json` (fed back to trader on next decision)
  - Sends Telegram summary

## Mode Switching

Set in `.env`:

```bash
TRADING_MODE=ai       # AI drives (this system)
TRADING_MODE=classic  # Old fixed-strategy crons (fallback)
```

Then `docker compose up -d --force-recreate` to apply.

AI mode stops the fixed scanners inside Docker and runs only monitor/EOD/summary as safety backups. The AI orchestrator on the Mac host handles all scanning and decisions.

## File Layout

```
ai/
├── mcp-trading.json       # MCP config for Claude CLI (written by setup.sh)
├── setup.sh               # Host installer
├── README.md
├── prompts/
│   ├── regime.md          # System prompt: market regime classifier
│   ├── scanner.md         # System prompt: setup scanner
│   ├── trader.md          # System prompt: trade decision
│   ├── manager.md         # System prompt: position management
│   └── reviewer.md        # System prompt: daily review
├── pine/                  # Pine Script verification templates
│   ├── divergence_check.pine
│   ├── sr_levels.pine
│   ├── pattern_flag.pine
│   └── volume_profile.pine
├── scripts/
│   ├── ai_common.sh       # Shared helpers (logging, API, Claude wrapper)
│   ├── ai_regime.sh       # */30 — regime classification
│   ├── ai_scan.sh         # */15 — setup scanner (triggers ai_trade.sh)
│   ├── ai_trade.sh        # on-demand — trade decision + execution
│   ├── ai_manage.sh       # */15 — position management
│   ├── ai_review.sh       # 21:05 — daily review
│   └── ai_eod.sh          # Fri 20:55 — EOD close
└── state/
    ├── regime.json        # Current regime (written by ai_regime)
    ├── insights.json      # Accumulated learnings (written by ai_review)
    └── trade_reasoning/   # Per-trade AI reasoning archive
```

## Quick Start

1. **Install TradingView MCP + configure Claude CLI:**
   ```bash
   bash ai/setup.sh
   ```

2. **Launch TradingView Desktop with remote debugging:**
   ```bash
   /Applications/TradingView.app/Contents/MacOS/TradingView --remote-debugging-port=9222
   ```

3. **Verify MCP connection:**
   ```bash
   claude -p "What symbol is the current TradingView chart showing?" \
     --mcp-config ai/mcp-trading.json \
     --allowedTools "mcp__tradingview" \
     --dangerously-skip-permissions
   ```

4. **Switch bot to AI mode:**
   ```bash
   # Edit .env:
   # TRADING_MODE=ai
   # conviction_high_risk_pct=0.5   ← small size for live testing
   # conviction_medium_risk_pct=0.25
   
   docker compose up -d --force-recreate
   ```

5. **Test each script manually:**
   ```bash
   bash ai/scripts/ai_regime.sh
   cat ai/state/regime.json
   
   bash ai/scripts/ai_scan.sh
   ls ai/state/trade_reasoning/
   ```

6. **Install host crontab** (see output of `setup.sh`).

## Live Testing Approach

**No paper mode.** Go live with minimum position sizes + tight structural stops. The trader prompt (trader.md) is tuned to err toward MEDIUM conviction during the proving phase, and the bot's `conviction_*_risk_pct` settings control actual risk.

Recommended live testing config in `.env`:
```bash
conviction_high_risk_pct=0.5        # was 3.0
conviction_medium_risk_pct=0.25     # was 3.0
conviction_low_risk_pct=0.1
MAX_POSITION_SIZE=1000              # forex minimum lot
max_daily_loss_percent=3.0          # tight daily limit during testing
max_weekly_loss_percent=6.0
```

Scale up `conviction_*_risk_pct` once the AI system has proven itself over 30+ live trades with positive expectancy.

## Cost Model (Claude Max $100/mo)

| Script | Frequency | Calls/day | Model |
|--------|-----------|-----------|-------|
| ai_regime | */30 during market hours | ~28 | Sonnet |
| ai_scan | */15 during market hours | ~56 | Sonnet |
| ai_trade | ~2-5 per day | ~5 | Opus |
| ai_manage | */15 when positions open | ~20 | Sonnet |
| ai_review | 1x daily | 1 | Opus |
| **Total** | | **~110/day** | |

## Safety Notes

- **Layer 0 limits are enforced in Python**, not prompts. AI cannot override them.
- `TRADING_MODE=classic` is always available as fallback — nothing about the fixed strategies has been removed.
- All AI reasoning is archived in `ai/state/trade_reasoning/` — full audit trail.
- Daily review compares AI predictions against outcomes — self-correcting via `insights.json`.
- Telegram notifications are sent by both the AI scripts AND the bot's `TradeExecutor` (redundant safety).

## Rolling Back to Classic Mode

1. Edit `.env`: `TRADING_MODE=classic`
2. `docker compose up -d --force-recreate`
3. Remove AI crontab entries on host: `crontab -e` and delete the Krabbe section

The fixed-strategy scoring engines and cron scripts are all still in place.
