# M5 Scalp Strategy Configurations

Optimal backtest-proven settings (2026-03-13, 60-day backtest window).

## Currently LIVE: NZDUSD (optimized 2026-03-13)

| Parameter | Value | Previous |
|---|---|---|
| `m5_signal_threshold` | **8.0** | 6.0 |
| `m5_high_conviction_threshold` | **11.0** | 9.0 |
| `atr_sl_multiplier` | **2.0** | 1.5 |
| `max_daily_loss_percent` | **6.0** | 3.0 |
| `max_weekly_loss_percent` | **15.0** | 6.0 |

- EOD close at 20:55 UTC, ratchet SL (no TP), conviction sizing ON
- Backtest: +20.2%/mo, PF 1.37, 25% max DD, ~41 trades/mo

**Why:** Higher threshold filters weak signals. Wider SL reduces noise stop-outs. Relaxed loss limits were blocking profitable recovery trades (old 3%/6% limits blocked 8,000+ signals).

## AUDUSD — Better than NZDUSD, ready to add

- Same optimized config as NZDUSD (thresh=8, SL=2xATR, relaxed limits)
- Backtest: **+26.5%/mo**, PF 1.43, 22.5% max DD, ~47 trades/mo
- Already in `instruments.py` with `broker="icmarkets"`
- To enable: add `"AUDUSD"` to `SCALP_INSTRUMENTS` in `cron/scan_scalp.sh`

**Why better:** Higher win rate (57% vs 54%), better PF, lower drawdown. Very similar pair but AUDUSD trends more cleanly.

## BOTH NZDUSD + AUDUSD — Maximum profit setup

| Setup | Monthly | PF | Max DD | Trades/mo |
|---|---|---|---|---|
| NZDUSD alone | +20.2% | 1.37 | 25.5% | 41 |
| AUDUSD alone | +26.5% | 1.43 | 22.5% | 47 |
| Both @ 1.5% risk each | **+31.4%** | 1.41 | 29.7% | 80 |
| Both @ half risk (1.125%) | +24.8% | 1.39 | 24.8% | 89 |

- ~25% of trades overlap (same direction within 30min) — partially correlated
- For dual-pair: set `conviction_medium_risk_pct` to 1.5 (from 2.25)
- **Min account for 1.5% risk:** ~270 EUR (below that, 1000-unit minimum forces higher risk)
- On 150 EUR account: 1000-unit minimum = ~2.67% actual risk per pair

## BTC M5 Scalp — Use DIFFERENT settings from FX

| Parameter | BTC value | FX value |
|---|---|---|
| `signal_threshold` | **6.0** | 8.0 |
| `high_conviction_threshold` | **9.0** | 11.0 |
| `atr_sl_multiplier` | **1.5** | 2.0 |

- Leverage: 1:2 (IC Markets retail BTC CFD)
- Min size: 0.01 BTC, size_round: 0.01
- Backtest: **+63.8%/mo**, PF 1.92, 7.5% max DD, ~137 trades/mo
- To enable: set `scalp_btc_enabled=true` in `.env.production`
- **Needs separate threshold handling** since BTC wants 6 while FX wants 8

**Why OLD settings for BTC:** BTC's high volatility means even "weaker" signals (threshold 6) are profitable. New threshold 8 cuts trades in half (138 vs 274) and halves returns. Wider SL (2xATR) hurts because BTC ATR is already massive.

## Pairs that DON'T work

- **USDJPY:** 1-6 trades in 2.7 months, all losses. Scoring engine doesn't generate signals.
- **CADJPY:** Same problem.
- **EURJPY:** Insufficient data. Not tested.
- JPY pairs generally incompatible with the M5 EMA/RSI/BB scoring factors.

## Key backtest learnings

- Smart EOD (let winners run overnight) HURTS — runners get stopped out overnight
- Regular EOD close is net positive — captures gains that would be given back
- Spread filter (40% of SL) rarely triggers — keep as safety net
- Cooldown (10min after 2 losses) has minimal effect — keep for safety
- No-loss-limits gives best raw returns but 35%+ drawdown — relaxed limits are the sweet spot
- Previous backtests showed inflated results due to parameter mismatches with live config
