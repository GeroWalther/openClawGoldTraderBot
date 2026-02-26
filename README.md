# Gold Trader Bot

Automated multi-instrument trading bot with Interactive Brokers, controlled via Telegram through an OpenClaw AI agent ("Krabbe"). Features multi-factor scoring engines, automated cron-based scanning, active trade management, and risk controls.

## Architecture

```
Telegram (User) <-> OpenClaw Gateway (VPS) <-> Claude LLM
                         |
                    Skills:
                    - market-analyst  (analysis, no trades)
                    - market-trader   (submit/cancel trades)
                    - trade-manager   (dashboard, modify, close)
                    - market-scanner  (trigger scans)
                    - send-email      (email reports)
                         |
                    Trading Bot (FastAPI, port 8001)
                         |
                    Interactive Brokers API (ib_async)
                         |
              Cron Jobs (scan, monitor, summarize)
```

## Instruments

| Key | Name | Type | Strategy | Exchange |
|-----|------|------|----------|----------|
| XAUUSD | Gold | CMDTY | krabbe_scored (11-factor) | SMART |
| BTC | Micro Bitcoin | FUT | rsi_reversal | CME |
| MES | Micro E-mini S&P | FUT | krabbe_scored | CME |
| EURUSD | EUR/USD | CASH | krabbe_scored | IDEALPRO |
| EURJPY | EUR/JPY | CASH | krabbe_scored | IDEALPRO |
| CADJPY | CAD/JPY | CASH | krabbe_scored | IDEALPRO |
| USDJPY | USD/JPY | CASH | krabbe_scored | IDEALPRO |
| IBUS500 | S&P 500 CFD | CFD | krabbe_scored | SMART |

## Scoring Engines

### Swing: Krabbe Scored (11-factor, daily bars)

Used by XAUUSD and most instruments. Combines technical + macro + sentiment:

| Factor | Weight | Source |
|--------|--------|--------|
| D1 Trend | 2.0 | SMA20/50/200 alignment + price position |
| 4H Momentum | 1.5 | MACD + RSI |
| 1H Entry | 1.0 | RSI mean-reversion |
| Chart Pattern | 1.5 | Breakout + Bollinger squeeze |
| TF Alignment | 0.5 | All SMAs same direction |
| S/R Proximity | 1.0 | Distance to pivot-clustered S/R levels |
| Fundamental 1 | 1.0 | DXY/VIX direction |
| Fundamental 2 | 1.0 | Yields/silver/SP500 |
| Fundamental 3 | 1.0 | Yield curve spread |
| News Sentiment | 1.0 | Yahoo RSS headline scoring |
| Calendar Risk | 1.0 | ForexFactory economic calendar |

Signal threshold: |score| >= 7. High conviction: |score| >= 12. Max score: 25.

### Swing: RSI Reversal (BTC)

Simpler strategy optimized for crypto (macro factors don't apply):

- **BUY**: RSI < 30 + price > SMA200
- **SELL**: RSI > 70 + price < SMA200
- Conviction: HIGH if RSI < 25 or > 75

### Intraday (6-factor, H1 + M15 bars)

Used for hourly scanning of XAUUSD and BTC:

| Factor | Weight | Source |
|--------|--------|--------|
| H1 Trend | 2.0 | SMA20/50 alignment |
| H1 Momentum | 1.5 | MACD + RSI |
| M15 Entry | 1.5 | RSI extremes on 15m |
| S/R Proximity | 1.0 | Pivot-clustered levels |
| Volatility | 1.0 | Bollinger bandwidth |
| Session Quality | 0.0 | Applied as multiplier (not additive) |

Session quality multiplier: London+NY overlap = 1.0x, major session = 0.85x, active = 0.70x, off-hours = 0.40x.

## S/R Levels

Support/resistance computed via swing pivot clustering (not naive 20-bar high/low):

1. Find swing highs/lows using `_find_swing_pivots()`
2. Cluster pivots within ATR tolerance
3. Rank by touch count (more touches = stronger level)
4. Top 3 support (below price) and resistance (above price) returned

## Signal-Type-Aware Entries

After scoring, signals are classified as `trend`, `mean_reversion`, or `mixed`:

| Signal Type | BUY Entry | SELL Entry |
|-------------|-----------|------------|
| trend | STOP above R1 (breakout) | STOP below S1 (breakdown) |
| mean_reversion | LIMIT at S1 | LIMIT at R1 |
| mixed | MARKET | MARKET |

If price is within 0.5 ATR of entry level, a MARKET order is used instead.

## Risk Controls

| Control | Setting |
|---------|---------|
| Risk per trade (HIGH) | 1.5% of account |
| Risk per trade (MEDIUM) | 1.0% of account |
| Risk per trade (LOW) | 0.75% of account |
| Daily loss limit | 3.0% of account |
| Weekly loss limit | 6.0% of account |
| Max daily trades | 5 |
| Spread protection | Reject if spread > 30% of SL |
| Cooldown | Exponential after 2+ consecutive losses |
| Correlation check | Block double risk on correlated instruments |

### Correlation Groups

Prevents same-direction trades on correlated instruments:

- **USD-sensitive**: XAUUSD, EURUSD (both rise when USD weakens)
- **JPY pairs**: EURJPY, CADJPY, USDJPY
- **S&P 500**: MES, IBUS500
- **Inverse pairs**: (XAUUSD, USDJPY), (EURUSD, USDJPY)

## Cron Jobs

All scripts in `cron/`, use shared helpers from `common.sh`. File locking prevents race conditions.

| Script | Schedule | Purpose |
|--------|----------|---------|
| `scan_intraday.sh` | Hourly 07-21 UTC, Mon-Fri | H1/M15 scan of XAUUSD + BTC |
| `scan_swing.sh` | 08:05, 13:05, 19:05 UTC, Mon-Fri | D1 scan of all instruments |
| `monitor.sh` | Every 30min 07-21 UTC, Mon-Fri | Active trade management |
| `daily_summary.sh` | 21:00 UTC, Mon-Fri | Daily P&L summary |

### Monitor Actions

- **75%+ to TP**: Trail SL to lock 50% of profit
- **50%+ to TP**: Move SL to breakeven (entry price)
- **70%+ to SL**: Send Telegram warning
- **Pending orders**: Cancel if expired (4h intraday, 24h swing)

## Project Structure

```
app/
  main.py                     # FastAPI app, lifespan (IBKR connection)
  config.py                   # Settings via pydantic-settings
  instruments.py              # Instrument specs (per-instrument strategy config)
  dependencies.py             # FastAPI dependency injection
  api/
    health.py                 # GET /health
    trades.py                 # POST /api/v1/trades/submit, /cancel
    positions.py              # GET /positions, /positions/status, /account
    technicals.py             # GET /technicals/scan, /{inst}, /{inst}/intraday
    journal.py                # GET/POST /journal, /journal/stats
    analytics.py              # GET /analytics, /analytics/cooldown
    backtest.py               # POST /backtest
  models/
    database.py               # SQLAlchemy async engine (SQLite)
    trade.py                  # Trade ORM model
    schemas.py                # Pydantic request/response schemas
  services/
    ibkr_client.py            # IBKR wrapper (ib_async, bracket orders)
    trade_executor.py         # Validate -> size -> execute -> log -> notify
    trade_validator.py        # SL/TP bounds, R:R ratio, size limits
    position_sizer.py         # Conviction-based risk sizing
    risk_manager.py           # Daily/weekly loss limits, unrealized PnL
    scoring_engine.py         # Krabbe 11-factor swing scorer
    intraday_scoring.py       # 6-factor intraday scorer
    technical_analyzer.py     # Multi-TF analysis orchestrator
    indicators.py             # SMA, RSI, ATR, MACD, Bollinger
    patterns.py               # Chart patterns, pivot S/R clustering
    macro_data.py             # DXY, VIX, yields, cross-asset data
    calendar.py               # ForexFactory economic calendar
    news.py                   # Yahoo RSS news sentiment
    backtester.py             # Strategy backtester (daily bars)
    telegram_notifier.py      # Telegram notifications
    journal_service.py        # Trade journal + analysis tracking
cron/
  common.sh                   # Shared helpers, locking, correlation check
  scan_intraday.sh            # Hourly intraday scanner
  scan_swing.sh               # 3x/day swing scanner
  monitor.sh                  # Trade monitor (SL management, trailing)
  daily_summary.sh            # End-of-day summary
  setup_cron.sh               # Install crontab entries
tests/                        # pytest + pytest-asyncio (309 tests)
openclaw-skills/              # OpenClaw AI agent skills
  SOUL.md                     # Krabbe personality
  market-analyst/SKILL.md     # Analysis skill
  market-trader/SKILL.md      # Trade execution skill
  trade-manager/SKILL.md      # Dashboard, modify, close
  market-scanner/SKILL.md     # Trigger scans
vps-status.sh                 # Local script for VPS dashboard
deploy.sh                     # rsync to VPS + restart
```

## API Endpoints

All endpoints require `x-api-key` header.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Service health check |
| GET | `/api/v1/positions/` | Open positions |
| GET | `/api/v1/positions/status` | Full dashboard (positions, orders, account, recent trades) |
| GET | `/api/v1/positions/account` | Account balance info |
| POST | `/api/v1/positions/close` | Close a position |
| POST | `/api/v1/positions/modify` | Modify SL/TP |
| POST | `/api/v1/trades/submit` | Submit new trade |
| POST | `/api/v1/trades/cancel` | Cancel pending order |
| GET | `/api/v1/technicals/scan` | Scan all instruments (swing) |
| GET | `/api/v1/technicals/{inst}` | Full multi-TF analysis |
| GET | `/api/v1/technicals/{inst}/intraday` | Intraday analysis (H1+M15) |
| GET | `/api/v1/analytics` | Trade performance analytics |
| GET | `/api/v1/analytics/cooldown` | Cooldown/loss limit status |
| GET | `/api/v1/journal` | Journal entries |
| GET | `/api/v1/journal/stats` | Journal accuracy stats |
| POST | `/api/v1/backtest` | Run strategy backtest |

## VPS Dashboard

```bash
bash vps-status.sh              # Full dashboard
bash vps-status.sh positions    # Positions + account
bash vps-status.sh scans        # Latest scan results
bash vps-status.sh journal      # Analytics + journal
bash vps-status.sh csv          # CSV scan history
bash vps-status.sh logs         # Cron logs + service status
```

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # fill in values
```

## Run

```bash
uvicorn app.main:app --reload --port 8001
```

## Test

```bash
pytest tests/ -x -q
```

## Deploy

```bash
bash deploy.sh
```

Syncs code + skills to VPS via rsync, sets up cron, restarts bot.

## Backtest Results (2y, 2024-2026)

| Instrument | Strategy | Trades | Win Rate | Profit Factor | Max DD | Return |
|------------|----------|--------|----------|---------------|--------|--------|
| XAUUSD | krabbe_scored | 31 | 74.2% | 3.69 | 2.3% | +23.7% |
| BTC | rsi_reversal | 20 | 70.0% | 2.91 | 7.8% | +43.7% |

## Stack

Python 3.11+ | FastAPI | ib_async | SQLAlchemy + aiosqlite | yfinance | pandas | httpx | pydantic-settings
