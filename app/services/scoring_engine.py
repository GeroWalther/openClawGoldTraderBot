"""Krabbe 12-factor weighted scoring engine for multi-factor backtesting.

Replicates the market-analyst SKILL.md scoring system programmatically.
Historical backtesting covers ~70-80% of factors. News sentiment and
calendar risk are unavailable historically and scored as neutral (0).
"""

import logging

import numpy as np
import pandas as pd

from app.services.macro_data import INSTRUMENT_MACRO_MAP

logger = logging.getLogger(__name__)

# Factor weights — tuned to avoid triple-counting SMA/RSI/MACD signals.
# tf_alignment and tv_technicals overlap heavily with d1_trend and 4h_momentum,
# so they're weighted at 0.5 to keep them as tie-breakers, not primary drivers.
FACTOR_WEIGHTS = {
    "d1_trend": 2.0,        # Primary trend — SMA alignment + price position
    "4h_momentum": 1.5,     # Momentum — MACD + RSI
    "1h_entry": 1.0,        # Entry timing — RSI mean-reversion
    "chart_pattern": 1.5,   # Breakout + Bollinger + candlestick/multi-bar patterns
    "tf_alignment": 0.5,    # Reduced: overlaps with d1_trend (same SMAs)
    "sr_proximity": 1.0,    # Support/resistance distance
    "tv_technicals": 0.5,   # Reduced: overlaps with d1_trend + 4h_momentum
    "fundamental_1": 1.0,   # DXY / VIX direction
    "fundamental_2": 1.0,   # Yields / silver / SP500
    "fundamental_3": 1.0,   # Yield curve spread
    "news_sentiment": 1.0,  # RSS headline sentiment
    "calendar_risk": 1.0,   # Economic calendar risk filter (can only subtract)
}

MAX_SCORE = sum(2 * w for w in FACTOR_WEIGHTS.values())  # 26


class ScoringEngine:
    """Replicates the market-analyst 11-factor weighted scoring system."""

    def score_bar(
        self,
        row: pd.Series,
        macro_row: pd.Series | None,
        instrument_key: str,
    ) -> dict:
        """Score a single daily bar using all available factors.

        Args:
            row: Daily OHLCV bar with computed indicators
                 (sma20, sma50, sma200, rsi, atr, macd, macd_signal,
                  macd_hist, bb_upper, bb_lower, bb_mid, high_20, low_20).
            macro_row: Macro data row aligned by date (may be None).
            instrument_key: Instrument key for correlation mapping.

        Returns:
            dict with total_score, conviction, direction, and per-factor scores.
        """
        factors = {}

        # Factor 1: D1 Trend — SMA20/50/200 alignment + price position (weight x2)
        factors["d1_trend"] = self._score_d1_trend(row)

        # Factor 2: 4H Momentum — approximated from daily MACD + RSI momentum (weight x1.5)
        factors["4h_momentum"] = self._score_4h_momentum(row)

        # Factor 3: 1H Entry — approximated from RSI mean-reversion (weight x1)
        factors["1h_entry"] = self._score_1h_entry(row)

        # Factor 4: Chart Pattern — 20-day breakout + Bollinger squeeze/expansion (weight x1.5)
        factors["chart_pattern"] = self._score_chart_pattern(row)

        # Factor 5: Timeframe Alignment — all SMAs same direction (weight x1)
        factors["tf_alignment"] = self._score_tf_alignment(row)

        # Factor 6: S/R Proximity — distance to 20-day high/low as % of ATR (weight x1)
        factors["sr_proximity"] = self._score_sr_proximity(row)

        # Factor 7: TV Technicals — approximated from indicator consensus (weight x1)
        factors["tv_technicals"] = self._score_tv_technicals(row)

        # Factor 8: Fundamental 1 — DXY/VIX 5-day change direction (weight x1)
        factors["fundamental_1"] = self._score_fundamental_1(macro_row, instrument_key)

        # Factor 9: Fundamental 2 — Yields/silver/SP500 (weight x1)
        factors["fundamental_2"] = self._score_fundamental_2(macro_row, instrument_key)

        # Factor 10: Fundamental 3 — Yield curve spread (weight x1)
        factors["fundamental_3"] = self._score_fundamental_3(macro_row, instrument_key)

        # Factor 11: News Sentiment — unavailable in backtest (weight x1)
        factors["news_sentiment"] = 0.0

        # Factor 12: Calendar Risk — unavailable in backtest (weight x1)
        factors["calendar_risk"] = 0.0

        # Compute weighted total
        total_score = sum(
            factors[f] * FACTOR_WEIGHTS[f] for f in factors
        )
        total_score = round(total_score, 2)

        # Direction and conviction
        if total_score >= 10:
            direction = "BUY"
        elif total_score <= -10:
            direction = "SELL"
        else:
            direction = None

        abs_score = abs(total_score)
        if abs_score >= 15:
            conviction = "HIGH"
        elif abs_score >= 10:
            conviction = "MEDIUM"
        else:
            conviction = None

        return {
            "total_score": total_score,
            "conviction": conviction,
            "direction": direction,
            "factors": factors,
        }

    def _score_d1_trend(self, row: pd.Series) -> float:
        """D1 trend from SMA alignment and price position. Range: -2 to +2."""
        score = 0.0
        close = row.get("close")
        sma20 = row.get("sma20")
        sma50 = row.get("sma50")
        sma200 = row.get("sma200")

        if any(pd.isna(v) for v in [close, sma20, sma50]):
            return 0.0

        # Price above/below SMA20
        if close > sma20:
            score += 0.5
        elif close < sma20:
            score -= 0.5

        # SMA20 > SMA50 (bullish)
        if sma20 > sma50:
            score += 0.5
        elif sma20 < sma50:
            score -= 0.5

        # SMA50 > SMA200 (long-term bullish)
        if not pd.isna(sma200):
            if sma50 > sma200:
                score += 0.5
            elif sma50 < sma200:
                score -= 0.5

            # Price above/below SMA200
            if close > sma200:
                score += 0.5
            elif close < sma200:
                score -= 0.5

        return max(-2.0, min(2.0, score))

    def _score_4h_momentum(self, row: pd.Series) -> float:
        """4H momentum approximated from daily MACD + RSI. Range: -2 to +2."""
        score = 0.0
        macd = row.get("macd")
        macd_signal = row.get("macd_signal")
        macd_hist = row.get("macd_hist")
        rsi = row.get("rsi")

        # MACD crossover direction
        if not pd.isna(macd) and not pd.isna(macd_signal):
            if macd > macd_signal:
                score += 1.0
            elif macd < macd_signal:
                score -= 1.0

        # MACD histogram momentum (growing/shrinking)
        if not pd.isna(macd_hist):
            if macd_hist > 0:
                score += 0.5
            elif macd_hist < 0:
                score -= 0.5

        # RSI momentum
        if not pd.isna(rsi):
            if rsi > 60:
                score += 0.5
            elif rsi < 40:
                score -= 0.5

        return max(-2.0, min(2.0, score))

    def _score_1h_entry(self, row: pd.Series) -> float:
        """1H entry approximated from RSI mean-reversion. Range: -2 to +2."""
        rsi = row.get("rsi")
        if pd.isna(rsi):
            return 0.0

        # Strong oversold = buy signal
        if rsi < 25:
            return 2.0
        elif rsi < 30:
            return 1.0
        # Strong overbought = sell signal
        elif rsi > 75:
            return -2.0
        elif rsi > 70:
            return -1.0
        # Neutral zone with slight bias
        elif rsi > 55:
            return 0.5
        elif rsi < 45:
            return -0.5

        return 0.0

    def _score_chart_pattern(self, row: pd.Series) -> float:
        """Chart pattern from breakout + Bollinger squeeze/expansion. Range: -2 to +2."""
        score = 0.0
        close = row.get("close")
        high_20 = row.get("high_20")
        low_20 = row.get("low_20")
        bb_upper = row.get("bb_upper")
        bb_lower = row.get("bb_lower")
        bb_mid = row.get("bb_mid")
        bb_bandwidth = row.get("bb_bandwidth")
        atr = row.get("atr")

        # 20-day high/low breakout
        if not any(pd.isna(v) for v in [close, high_20, low_20] if v is not None):
            if not pd.isna(high_20) and close >= high_20:
                score += 1.0  # Breakout above 20-day high
            elif not pd.isna(low_20) and close <= low_20:
                score -= 1.0  # Breakdown below 20-day low

        # Bollinger Band position
        if not pd.isna(bb_upper) and not pd.isna(bb_lower) and not pd.isna(close):
            if close > bb_upper:
                score += 0.5  # Expansion breakout bullish
            elif close < bb_lower:
                score -= 0.5  # Expansion breakout bearish
            elif not pd.isna(bb_mid):
                if close > bb_mid:
                    score += 0.25
                elif close < bb_mid:
                    score -= 0.25

        # Bollinger squeeze (low bandwidth = potential breakout)
        if not pd.isna(bb_bandwidth):
            if bb_bandwidth < 0.02:
                # Squeeze detected — direction from price vs mid
                if not pd.isna(bb_mid) and not pd.isna(close):
                    if close > bb_mid:
                        score += 0.5
                    else:
                        score -= 0.5

        return max(-2.0, min(2.0, score))

    def _score_tf_alignment(self, row: pd.Series) -> float:
        """Timeframe alignment — all SMAs trending same direction. Range: -2 to +2."""
        sma20 = row.get("sma20")
        sma50 = row.get("sma50")
        sma200 = row.get("sma200")

        if any(pd.isna(v) for v in [sma20, sma50]):
            return 0.0

        if pd.isna(sma200):
            # Only 2 SMAs available
            if sma20 > sma50:
                return 1.0
            elif sma20 < sma50:
                return -1.0
            return 0.0

        # All three aligned bullish: SMA20 > SMA50 > SMA200
        if sma20 > sma50 > sma200:
            return 2.0
        # All three aligned bearish
        elif sma20 < sma50 < sma200:
            return -2.0
        # Partially aligned
        elif sma20 > sma50:
            return 1.0
        elif sma20 < sma50:
            return -1.0

        return 0.0

    def _score_sr_proximity(self, row: pd.Series) -> float:
        """S/R proximity — distance to 20-day high/low as % of ATR. Range: -2 to +2."""
        close = row.get("close")
        high_20 = row.get("high_20")
        low_20 = row.get("low_20")
        atr = row.get("atr")

        if any(pd.isna(v) for v in [close, high_20, low_20, atr]) or atr == 0:
            return 0.0

        dist_to_high = (high_20 - close) / atr
        dist_to_low = (close - low_20) / atr

        # Near support (good for buying)
        if dist_to_low < 0.5:
            return 1.5  # Close to support = good long entry
        elif dist_to_low < 1.0:
            return 0.5

        # Near resistance (good for selling)
        if dist_to_high < 0.5:
            return -1.5  # Close to resistance = good short entry
        elif dist_to_high < 1.0:
            return -0.5

        return 0.0

    def _score_tv_technicals(self, row: pd.Series) -> float:
        """TV technicals approximated from indicator consensus. Range: -2 to +2."""
        votes = 0
        count = 0

        # SMA vote
        close = row.get("close")
        sma20 = row.get("sma20")
        sma50 = row.get("sma50")

        if not pd.isna(close) and not pd.isna(sma20):
            votes += 1 if close > sma20 else -1
            count += 1
        if not pd.isna(close) and not pd.isna(sma50):
            votes += 1 if close > sma50 else -1
            count += 1

        # RSI vote
        rsi = row.get("rsi")
        if not pd.isna(rsi):
            if rsi > 60:
                votes += 1
            elif rsi < 40:
                votes -= 1
            count += 1

        # MACD vote
        macd = row.get("macd")
        macd_signal = row.get("macd_signal")
        if not pd.isna(macd) and not pd.isna(macd_signal):
            votes += 1 if macd > macd_signal else -1
            count += 1

        if count == 0:
            return 0.0

        consensus = votes / count  # -1 to +1
        return round(max(-2.0, min(2.0, consensus * 2)), 2)

    def _score_fundamental_1(self, macro_row: pd.Series | None, instrument_key: str) -> float:
        """Fundamental factor 1: primary macro indicator direction. Range: -2 to +2."""
        if macro_row is None:
            return 0.0

        correlations = INSTRUMENT_MACRO_MAP.get(instrument_key.upper(), {})

        # Find primary fundamental (DXY for most, VIX for equities)
        primary = None
        if "DX-Y.NYB" in correlations:
            primary = ("DX-Y.NYB", correlations["DX-Y.NYB"])
        elif "^VIX" in correlations:
            primary = ("^VIX", correlations["^VIX"])

        if primary is None:
            return 0.0

        ticker, correlation = primary
        change_col = f"{ticker}_change5"

        if change_col not in macro_row.index or pd.isna(macro_row.get(change_col)):
            return 0.0

        change = float(macro_row[change_col])

        # Determine score based on change direction and correlation
        if abs(change) < 0.01:
            return 0.0

        direction_score = 1.0 if change > 0 else -1.0

        # Invert if correlation is inverse
        if correlation == "inverse":
            direction_score *= -1

        # Scale by magnitude
        if abs(change) > 2.0:  # Strong move
            direction_score *= 2.0

        return max(-2.0, min(2.0, direction_score))

    def _score_fundamental_2(self, macro_row: pd.Series | None, instrument_key: str) -> float:
        """Fundamental factor 2: secondary macro indicator. Range: -2 to +2."""
        if macro_row is None:
            return 0.0

        correlations = INSTRUMENT_MACRO_MAP.get(instrument_key.upper(), {})

        # Find secondary fundamental (yields for gold, SP500 for equities, etc.)
        secondary = None
        for ticker, corr in correlations.items():
            if ticker not in ("DX-Y.NYB", "^VIX"):
                secondary = (ticker, corr)
                break

        if secondary is None:
            return 0.0

        ticker, correlation = secondary
        change_col = f"{ticker}_change5"

        if change_col not in macro_row.index or pd.isna(macro_row.get(change_col)):
            return 0.0

        change = float(macro_row[change_col])

        if abs(change) < 0.01:
            return 0.0

        direction_score = 1.0 if change > 0 else -1.0

        if correlation == "inverse":
            direction_score *= -1

        if abs(change) > 2.0:
            direction_score *= 2.0

        return max(-2.0, min(2.0, direction_score))

    def _score_fundamental_3(self, macro_row: pd.Series | None, instrument_key: str) -> float:
        """Fundamental factor 3: yield curve spread (10Y - 13W T-bill). Range: -2 to +2."""
        if macro_row is None:
            return 0.0

        correlations = INSTRUMENT_MACRO_MAP.get(instrument_key.upper(), {})
        if "yield_curve" not in correlations:
            return 0.0

        correlation = correlations["yield_curve"]
        change_col = "yield_curve_change5"

        if change_col not in macro_row.index or pd.isna(macro_row.get(change_col)):
            return 0.0

        change = float(macro_row[change_col])

        # Yield curve moves are smaller than DXY — use lower threshold
        if abs(change) < 0.005:
            return 0.0

        direction_score = 1.0 if change > 0 else -1.0

        if correlation == "inverse":
            direction_score *= -1

        # Strong move threshold for yield curve
        if abs(change) > 0.5:
            direction_score *= 2.0

        return max(-2.0, min(2.0, direction_score))
