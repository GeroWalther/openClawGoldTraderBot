# Gold Trader Bot

Automated XAUUSD trading bot with Interactive Brokers API, controlled via Telegram through an OpenClaw AI agent ("Krabbe").

## Architecture

```
Telegram (User) <-> OpenClaw Gateway (VPS) <-> Claude LLM
                         |
                    Skills:
                    - gold-analyst  (market analysis)
                    - gold-trader   (submit trades)
                    - claude-cli    (coding/system tasks)
                         |
                    Trading Bot (FastAPI, port 8001)
                         |
                    Interactive Brokers API (ib_async)
```

## Project Structure

```
app/
  main.py                  # FastAPI app, lifespan (IBKR connection)
  config.py                # Settings via pydantic-settings (.env)
  dependencies.py          # FastAPI dependency injection
  api/
    health.py              # GET /health
    trades.py              # POST /api/v1/trades/submit
    positions.py           # GET /api/v1/positions, /positions/account
  models/
    database.py            # SQLAlchemy async engine
    trade.py               # Trade ORM model
    schemas.py             # Pydantic request/response schemas
  services/
    ibkr_client.py         # IBKR wrapper (ib_async, bracket orders)
    trade_validator.py     # SL/TP bounds, R:R ratio, size limits
    position_sizer.py      # Risk-based sizing (1% of account)
    trade_executor.py      # Validate -> size -> execute -> log -> notify
    telegram_notifier.py   # Trade notifications via Telegram Bot API
  utils/
    rate_limiter.py        # Sliding window rate limiter
tests/                     # pytest + pytest-asyncio
openclaw-skills/
  SOUL.md                  # Krabbe personality (general assistant + trading)
  gold-analyst/SKILL.md    # Market analysis skill
  gold-trader/SKILL.md     # Trade execution skill
  claude-cli/SKILL.md      # Claude Code CLI skill
deploy.sh                  # rsync to VPS + restart bot
```

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env       # fill in your values
```

## Run

```bash
uvicorn app.main:app --reload --port 8001
```

## Test

```bash
pytest tests/ -v
```

## Deploy to VPS

```bash
./deploy.sh
```

Syncs code + OpenClaw skills to VPS via rsync, restarts the bot.

## Trading Rules

- No trade without user confirmation
- Stop-loss required on every trade
- Min R:R ratio 1:1
- Max risk per trade: 1% of account
- Min position: 1 troy ounce (IBKR minimum)
- XAUUSD contract: symbol=XAUUSD, secType=CMDTY, exchange=SMART

## Stack

Python 3.11+ | FastAPI | ib_async | SQLAlchemy + aiosqlite | python-telegram-bot | pydantic-settings
