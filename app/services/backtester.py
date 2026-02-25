import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

from app.instruments import InstrumentSpec, get_instrument
from app.services.scoring_engine import ScoringEngine

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    entry_date: str
    exit_date: str
    direction: str
    entry_price: float
    exit_price: float
    sl_price: float
    tp_price: float
    size: float
    pnl: float
    conviction: str
    exit_reason: str  # "tp", "sl", "tp1_partial", "end_of_data"


class Backtester:
    """Strategy backtester using historical OHLC data from yfinance."""

    # FX tickers for converting quote currency to USD
    FX_TICKERS = {
        "JPY": "JPY=X",      # USDJPY — divide PnL by this rate
        "GBP": "GBPUSD=X",   # GBPUSD — multiply PnL by this rate
    }

    def run(
        self,
        instrument_key: str,
        strategy: str,
        period: str = "1y",
        initial_balance: float = 10000.0,
        risk_percent: float = 3.0,
        atr_sl_multiplier: float = 1.5,
        atr_tp_multiplier: float = 2.0,
        session_filter: bool = True,
        partial_tp: bool = True,
        macro_service=None,
        start_date: str | None = None,
        end_date: str | None = None,
        max_trades: int | None = None,
    ) -> dict:
        """Run a backtest and return results."""
        instrument = get_instrument(instrument_key)
        df = self._fetch_data(instrument, period, start_date=start_date, end_date=end_date)
        if df is None or len(df) < 60:
            return {"error": f"Insufficient data for {instrument_key} ({period})"}

        # Compute indicators
        df = self._compute_indicators(df)

        # Fetch FX conversion rate for non-USD quote currencies
        fx_map = self._fetch_fx_series(instrument, period, start_date, end_date)

        # Generate signals
        if strategy == "krabbe_scored":
            macro_df = None
            if macro_service is not None:
                # Use longer period for macro data to ensure coverage
                macro_period = "5y" if start_date else period
                macro_df = macro_service.get_macro_series(instrument_key, macro_period)
            signals = self._krabbe_scored_signals(df, macro_df, instrument_key)
        else:
            signals = self._generate_signals(df, strategy)

        # Simulate trades
        trades, equity_curve = self._simulate(
            df, signals, instrument,
            initial_balance=initial_balance,
            risk_percent=risk_percent,
            atr_sl_mult=atr_sl_multiplier,
            atr_tp_mult=atr_tp_multiplier,
            session_filter=session_filter,
            partial_tp=partial_tp,
            max_trades=max_trades,
            fx_map=fx_map,
        )

        return self._compile_results(
            instrument_key, strategy, period,
            initial_balance, trades, equity_curve,
        )

    def _fetch_data(
        self, instrument: InstrumentSpec, period: str,
        start_date: str | None = None, end_date: str | None = None,
    ) -> pd.DataFrame | None:
        try:
            ticker = yf.Ticker(instrument.yahoo_symbol)
            if start_date and end_date:
                df = ticker.history(start=start_date, end=end_date, interval="1d")
            elif start_date:
                df = ticker.history(start=start_date, interval="1d")
            else:
                df = ticker.history(period=period, interval="1d")
            if df is None or df.empty:
                return None
            df = df.reset_index()
            df.columns = [c.lower() for c in df.columns]
            return df
        except Exception as e:
            logger.warning("Backtest data fetch failed for %s: %s", instrument.key, e)
            return None

    def _fetch_fx_series(
        self, instrument: InstrumentSpec, period: str,
        start_date: str | None = None, end_date: str | None = None,
    ) -> dict[object, float] | None:
        """Fetch daily quote-to-USD conversion factors for non-USD instruments.

        Returns a dict mapping date → factor where: pnl_usd = pnl_quote * factor.
        For JPY: factor = 1/USDJPY (e.g. 1/150 ≈ 0.00667).
        For GBP: factor = GBPUSD (e.g. 1.27).
        Returns None for USD-denominated instruments.
        """
        if instrument.currency == "USD":
            return None

        ticker_sym = self.FX_TICKERS.get(instrument.currency)
        if not ticker_sym:
            logger.warning("No FX ticker for currency %s — PnL will be in quote currency", instrument.currency)
            return None

        try:
            ticker = yf.Ticker(ticker_sym)
            if start_date and end_date:
                fx_df = ticker.history(start=start_date, end=end_date, interval="1d")
            elif start_date:
                fx_df = ticker.history(start=start_date, interval="1d")
            else:
                fx_df = ticker.history(period=period, interval="1d")

            if fx_df is None or fx_df.empty:
                return None

            fx_df = fx_df.reset_index()
            fx_df.columns = [c.lower() for c in fx_df.columns]

            fx_map: dict[object, float] = {}
            for _, row in fx_df.iterrows():
                dt = row["date"]
                date_key = dt.date() if hasattr(dt, "date") else dt
                rate = row["close"]
                if instrument.currency == "JPY":
                    fx_map[date_key] = 1.0 / rate  # 1 JPY → USD
                else:
                    fx_map[date_key] = rate  # e.g. 1 GBP → USD
            return fx_map
        except Exception as e:
            logger.warning("FX rate fetch failed for %s: %s", instrument.currency, e)
            return None

    def _compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        from app.services.indicators import compute_indicators

        return compute_indicators(df)

    def _generate_signals(self, df: pd.DataFrame, strategy: str) -> list[dict]:
        """Generate entry signals based on strategy."""
        signals = []

        if strategy == "sma_crossover":
            signals = self._sma_crossover_signals(df)
        elif strategy == "rsi_reversal":
            signals = self._rsi_reversal_signals(df)
        elif strategy == "breakout":
            signals = self._breakout_signals(df)

        return signals

    def _sma_crossover_signals(self, df: pd.DataFrame) -> list[dict]:
        signals = []
        for i in range(51, len(df)):
            row = df.iloc[i]
            prev = df.iloc[i - 1]

            if pd.isna(row["sma20"]) or pd.isna(row["sma50"]):
                continue

            # Bullish crossover: SMA20 crosses above SMA50
            if prev["sma20"] <= prev["sma50"] and row["sma20"] > row["sma50"]:
                conviction = "HIGH" if (not pd.isna(row.get("sma200")) and row["close"] > row["sma200"]) else "MEDIUM"
                signals.append({
                    "index": i, "direction": "BUY", "conviction": conviction,
                    "price": row["close"], "atr": row["atr"],
                })
            # Bearish crossover: SMA20 crosses below SMA50
            elif prev["sma20"] >= prev["sma50"] and row["sma20"] < row["sma50"]:
                conviction = "HIGH" if (not pd.isna(row.get("sma200")) and row["close"] < row["sma200"]) else "MEDIUM"
                signals.append({
                    "index": i, "direction": "SELL", "conviction": conviction,
                    "price": row["close"], "atr": row["atr"],
                })

        return signals

    def _rsi_reversal_signals(self, df: pd.DataFrame) -> list[dict]:
        signals = []
        for i in range(201, len(df)):
            row = df.iloc[i]
            if pd.isna(row["rsi"]) or pd.isna(row["sma200"]):
                continue

            # RSI < 30 + price above SMA200 = BUY
            if row["rsi"] < 30 and row["close"] > row["sma200"]:
                conviction = "HIGH" if row["rsi"] < 25 else "MEDIUM"
                signals.append({
                    "index": i, "direction": "BUY", "conviction": conviction,
                    "price": row["close"], "atr": row["atr"],
                })
            # RSI > 70 + price below SMA200 = SELL
            elif row["rsi"] > 70 and row["close"] < row["sma200"]:
                conviction = "HIGH" if row["rsi"] > 75 else "MEDIUM"
                signals.append({
                    "index": i, "direction": "SELL", "conviction": conviction,
                    "price": row["close"], "atr": row["atr"],
                })

        return signals

    def _krabbe_scored_signals(
        self, df: pd.DataFrame, macro_df: pd.DataFrame | None, instrument_key: str,
    ) -> list[dict]:
        """Generate signals using the Krabbe 11-factor scoring engine."""
        engine = ScoringEngine()
        signals = []
        last_signal_idx = -999
        last_signal_dir = None

        # Align macro data by date if available
        macro_aligned = {}
        if macro_df is not None and not macro_df.empty:
            for idx_val, macro_row in macro_df.iterrows():
                date_key = idx_val
                if hasattr(date_key, "date"):
                    date_key = date_key.date()
                macro_aligned[date_key] = macro_row

        warmup = 50  # Need SMA50 at minimum
        for i in range(warmup, len(df)):
            row = df.iloc[i]

            if pd.isna(row.get("sma50")) or pd.isna(row.get("atr")):
                continue

            # Find matching macro row by date
            macro_row = None
            bar_date = row.get("date")
            if bar_date is not None and macro_aligned:
                if hasattr(bar_date, "date"):
                    bar_date_key = bar_date.date()
                else:
                    bar_date_key = bar_date
                macro_row = macro_aligned.get(bar_date_key)

            result = engine.score_bar(row, macro_row, instrument_key)

            direction = result["direction"]
            if direction is None:
                continue

            # Debounce: skip if same direction within 5 bars
            if direction == last_signal_dir and (i - last_signal_idx) < 5:
                continue

            conviction = result["conviction"] or "MEDIUM"

            signals.append({
                "index": i,
                "direction": direction,
                "conviction": conviction,
                "price": row["close"],
                "atr": row["atr"],
                "score": result["total_score"],
            })

            last_signal_idx = i
            last_signal_dir = direction

        return signals

    def _breakout_signals(self, df: pd.DataFrame) -> list[dict]:
        signals = []
        for i in range(21, len(df)):
            row = df.iloc[i]
            prev = df.iloc[i - 1]

            if pd.isna(row["high_20"]) or pd.isna(row["atr"]):
                continue

            prev_high_20 = df.iloc[i - 1]["high_20"] if not pd.isna(df.iloc[i - 1]["high_20"]) else None
            prev_low_20 = df.iloc[i - 1]["low_20"] if not pd.isna(df.iloc[i - 1]["low_20"]) else None

            if prev_high_20 is None or prev_low_20 is None:
                continue

            # Breakout above 20-day high with ATR confirmation
            if row["close"] > prev_high_20 and (row["close"] - prev_high_20) > row["atr"] * 0.5:
                conviction = "HIGH" if (row["close"] - prev_high_20) > row["atr"] else "MEDIUM"
                signals.append({
                    "index": i, "direction": "BUY", "conviction": conviction,
                    "price": row["close"], "atr": row["atr"],
                })
            # Breakdown below 20-day low
            elif row["close"] < prev_low_20 and (prev_low_20 - row["close"]) > row["atr"] * 0.5:
                conviction = "HIGH" if (prev_low_20 - row["close"]) > row["atr"] else "MEDIUM"
                signals.append({
                    "index": i, "direction": "SELL", "conviction": conviction,
                    "price": row["close"], "atr": row["atr"],
                })

        return signals

    def _simulate(
        self,
        df: pd.DataFrame,
        signals: list[dict],
        instrument: InstrumentSpec,
        initial_balance: float,
        risk_percent: float,
        atr_sl_mult: float,
        atr_tp_mult: float,
        session_filter: bool,
        partial_tp: bool,
        max_trades: int | None = None,
        fx_map: dict[object, float] | None = None,
    ) -> tuple[list[BacktestTrade], list[dict]]:
        balance = initial_balance
        trades: list[BacktestTrade] = []
        equity_curve = [{"date": df.iloc[0]["date"].isoformat() if hasattr(df.iloc[0]["date"], "isoformat") else str(df.iloc[0]["date"]), "equity": balance}]

        in_position = False
        cooldown_until = -1
        consecutive_losses = 0
        daily_trades: dict[str, int] = {}

        # Pre-compute fallback FX factor (last available rate)
        _fx_fallback = list(fx_map.values())[-1] if fx_map else 1.0

        def _get_fx(bar_date) -> float:
            """Get quote-to-USD conversion factor for a given date."""
            if fx_map is None:
                return 1.0
            date_key = bar_date.date() if hasattr(bar_date, "date") else bar_date
            return fx_map.get(date_key, _fx_fallback)

        for signal in signals:
            # Stop if max trades reached
            if max_trades is not None and len(trades) >= max_trades:
                break

            idx = signal["index"]
            if idx >= len(df) - 1:
                continue

            # Skip if in position
            if in_position:
                continue

            # Cooldown check
            if idx < cooldown_until:
                continue

            # Simple daily trade count check
            date_key = str(df.iloc[idx]["date"])[:10]
            if daily_trades.get(date_key, 0) >= 5:
                continue

            atr = signal.get("atr")
            if pd.isna(atr) or atr is None or atr <= 0:
                continue

            # Session filter: skip weekends for instruments with sessions
            if session_filter and instrument.trading_sessions:
                dt = df.iloc[idx]["date"]
                if hasattr(dt, "weekday") and dt.weekday() >= 5:
                    continue

            # Calculate SL/TP
            sl_dist = max(atr * atr_sl_mult, instrument.min_stop_distance)
            sl_dist = min(sl_dist, instrument.max_stop_distance)
            tp_dist = atr * atr_tp_mult
            tp_dist = max(tp_dist, sl_dist)  # minimum 1:1

            entry_price = signal["price"]
            direction = signal["direction"]

            if direction == "BUY":
                sl_price = entry_price - sl_dist
                tp_price = entry_price + tp_dist
            else:
                sl_price = entry_price + sl_dist
                tp_price = entry_price - tp_dist

            # Position sizing
            conviction = signal.get("conviction", "MEDIUM")
            if conviction == "HIGH":
                risk_pct = risk_percent
            elif conviction == "MEDIUM":
                risk_pct = risk_percent * 0.75
            else:
                risk_pct = risk_percent * 0.5

            risk_amount = balance * (risk_pct / 100)
            # Convert SL distance to USD for proper cross-currency sizing
            entry_fx = _get_fx(df.iloc[idx]["date"])
            sl_dist_usd = sl_dist * entry_fx * instrument.multiplier
            raw_size = risk_amount / sl_dist_usd if sl_dist_usd > 0 else 0

            # Round appropriately per asset class (no broker min_size floor for backtesting)
            if instrument.sec_type == "CASH":
                size = max(round(raw_size / 1000) * 1000, 1000)
            elif instrument.sec_type == "FUT":
                size = max(round(raw_size), 1)
            else:
                size = max(round(raw_size, 1), 0.1)

            # Simulate trade outcome using future bars
            in_position = True
            exit_price = None
            exit_reason = "end_of_data"
            exit_idx = len(df) - 1

            # Partial TP tracking
            partial_filled = False
            partial_pnl = 0.0

            for j in range(idx + 1, len(df)):
                bar = df.iloc[j]

                if direction == "BUY":
                    # Check SL
                    if bar["low"] <= sl_price:
                        if partial_tp and partial_filled:
                            # Remaining half stopped out
                            exit_price = sl_price
                            exit_reason = "sl"
                            exit_idx = j
                            break
                        exit_price = sl_price
                        exit_reason = "sl"
                        exit_idx = j
                        break
                    # Check partial TP (at 1R)
                    if partial_tp and not partial_filled:
                        tp1_price = entry_price + sl_dist  # 1R
                        if bar["high"] >= tp1_price:
                            partial_pnl = (tp1_price - entry_price) * (size * 0.5) * instrument.multiplier
                            partial_filled = True
                    # Check full TP
                    if bar["high"] >= tp_price:
                        exit_price = tp_price
                        exit_reason = "tp"
                        exit_idx = j
                        break
                else:  # SELL
                    if bar["high"] >= sl_price:
                        exit_price = sl_price
                        exit_reason = "sl"
                        exit_idx = j
                        break
                    if partial_tp and not partial_filled:
                        tp1_price = entry_price - sl_dist
                        if bar["low"] <= tp1_price:
                            partial_pnl = (entry_price - tp1_price) * (size * 0.5) * instrument.multiplier
                            partial_filled = True
                    if bar["low"] <= tp_price:
                        exit_price = tp_price
                        exit_reason = "tp"
                        exit_idx = j
                        break

            if exit_price is None:
                exit_price = df.iloc[exit_idx]["close"]

            # Calculate P&L (in quote currency, then convert to USD)
            exit_fx = _get_fx(df.iloc[exit_idx]["date"])

            if partial_tp and partial_filled:
                if exit_reason == "sl":
                    # Half won at TP1, half lost at SL
                    remaining_size = size * 0.5
                    if direction == "BUY":
                        remaining_pnl = (exit_price - entry_price) * remaining_size * instrument.multiplier
                    else:
                        remaining_pnl = (entry_price - exit_price) * remaining_size * instrument.multiplier
                    pnl = (partial_pnl + remaining_pnl) * exit_fx
                else:
                    # Both halves profitable
                    if direction == "BUY":
                        remaining_pnl = (exit_price - entry_price) * (size * 0.5) * instrument.multiplier
                    else:
                        remaining_pnl = (entry_price - exit_price) * (size * 0.5) * instrument.multiplier
                    pnl = (partial_pnl + remaining_pnl) * exit_fx
            else:
                if direction == "BUY":
                    pnl = (exit_price - entry_price) * size * instrument.multiplier * exit_fx
                else:
                    pnl = (entry_price - exit_price) * size * instrument.multiplier * exit_fx

            balance += pnl
            in_position = False

            # Track daily count
            daily_trades[date_key] = daily_trades.get(date_key, 0) + 1

            # Cooldown logic
            if pnl < 0:
                consecutive_losses += 1
                if consecutive_losses >= 2:
                    cooldown_bars = 2 * (2 ** (consecutive_losses - 2))
                    cooldown_until = exit_idx + cooldown_bars
            else:
                consecutive_losses = 0

            exit_date = df.iloc[exit_idx]["date"]
            trades.append(BacktestTrade(
                entry_date=df.iloc[idx]["date"].isoformat() if hasattr(df.iloc[idx]["date"], "isoformat") else str(df.iloc[idx]["date"]),
                exit_date=exit_date.isoformat() if hasattr(exit_date, "isoformat") else str(exit_date),
                direction=direction,
                entry_price=round(entry_price, 4),
                exit_price=round(exit_price, 4),
                sl_price=round(sl_price, 4),
                tp_price=round(tp_price, 4),
                size=size,
                pnl=round(pnl, 2),
                conviction=conviction,
                exit_reason=exit_reason,
            ))

            equity_curve.append({
                "date": exit_date.isoformat() if hasattr(exit_date, "isoformat") else str(exit_date),
                "equity": round(balance, 2),
            })

        return trades, equity_curve

    def _compile_results(
        self,
        instrument_key: str,
        strategy: str,
        period: str,
        initial_balance: float,
        trades: list[BacktestTrade],
        equity_curve: list[dict],
    ) -> dict:
        if not trades:
            return {
                "instrument": instrument_key,
                "strategy": strategy,
                "period": period,
                "initial_balance": initial_balance,
                "final_balance": initial_balance,
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "win_rate": 0.0,
                "expectancy": 0.0,
                "profit_factor": 0.0,
                "max_drawdown": 0.0,
                "total_return_pct": 0.0,
                "trades": [],
                "equity_curve": equity_curve,
                "monthly_breakdown": [],
            }

        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl < 0]

        total = len(trades)
        win_rate = len(wins) / total * 100 if total > 0 else 0
        avg_win = sum(t.pnl for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t.pnl for t in losses) / len(losses) if losses else 0

        win_pct = len(wins) / total if total > 0 else 0
        loss_pct = len(losses) / total if total > 0 else 0
        expectancy = (win_pct * avg_win) + (loss_pct * avg_loss)

        gross_profit = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (999.99 if gross_profit > 0 else 0.0)

        # Max drawdown from equity curve
        peak = initial_balance
        max_dd = 0
        for point in equity_curve:
            eq = point["equity"]
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak * 100 if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd

        final_balance = equity_curve[-1]["equity"] if equity_curve else initial_balance
        total_return = (final_balance - initial_balance) / initial_balance * 100

        # Monthly breakdown
        from collections import defaultdict
        monthly: dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0})
        for t in trades:
            month_key = t.exit_date[:7]
            monthly[month_key]["pnl"] += t.pnl
            monthly[month_key]["trades"] += 1
            if t.pnl > 0:
                monthly[month_key]["wins"] += 1

        monthly_breakdown = [
            {"month": k, "pnl": round(v["pnl"], 2), "trades": v["trades"], "win_rate": round(v["wins"] / v["trades"] * 100, 1) if v["trades"] > 0 else 0}
            for k, v in sorted(monthly.items())
        ]

        return {
            "instrument": instrument_key,
            "strategy": strategy,
            "period": period,
            "initial_balance": initial_balance,
            "final_balance": round(final_balance, 2),
            "total_trades": total,
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate": round(win_rate, 2),
            "expectancy": round(expectancy, 2),
            "profit_factor": round(profit_factor, 2),
            "max_drawdown": round(max_dd, 2),
            "total_return_pct": round(total_return, 2),
            "trades": [
                {
                    "entry_date": t.entry_date,
                    "exit_date": t.exit_date,
                    "direction": t.direction,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "sl_price": t.sl_price,
                    "tp_price": t.tp_price,
                    "size": t.size,
                    "pnl": t.pnl,
                    "conviction": t.conviction,
                    "exit_reason": t.exit_reason,
                }
                for t in trades
            ],
            "equity_curve": equity_curve,
            "monthly_breakdown": monthly_breakdown,
        }
