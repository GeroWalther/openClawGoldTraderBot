"""Tests for chart pattern detection service."""

import numpy as np
import pandas as pd
import pytest

from app.services.patterns import (
    _detect_candlestick_patterns,
    _detect_chart_patterns,
    _detect_trend_structure,
    _detect_trendline,
    detect_patterns,
)


def _make_ohlcv(n: int = 50, base_price: float = 100.0, seed: int = 42) -> pd.DataFrame:
    """Create synthetic OHLCV DataFrame."""
    np.random.seed(seed)
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    close = base_price + np.random.randn(n).cumsum()
    high = close + np.abs(np.random.randn(n)) * 2
    low = close - np.abs(np.random.randn(n)) * 2
    open_ = close + np.random.randn(n) * 0.5
    volume = np.random.randint(1000, 10000, n)
    return pd.DataFrame({
        "open": open_, "high": high, "low": low, "close": close, "volume": volume,
    }, index=dates)


class TestCandlestickPatterns:
    def test_bullish_engulfing(self):
        """Bullish engulfing: prev bearish, curr bullish engulfs prev."""
        df = pd.DataFrame({
            "open": [105, 103, 97],
            "high": [106, 104, 106],
            "low": [99, 96, 96],
            "close": [100, 97, 105],
        })
        patterns = _detect_candlestick_patterns(df)
        names = [p["name"] for p in patterns]
        assert "bullish_engulfing" in names

    def test_bearish_engulfing(self):
        """Bearish engulfing: prev bullish, curr bearish engulfs prev."""
        df = pd.DataFrame({
            "open": [95, 97, 103],
            "high": [104, 104, 104],
            "low": [94, 96, 94],
            "close": [100, 103, 95],
        })
        patterns = _detect_candlestick_patterns(df)
        names = [p["name"] for p in patterns]
        assert "bearish_engulfing" in names

    def test_doji(self):
        """Doji: very small body relative to range."""
        df = pd.DataFrame({
            "open": [90, 95, 100.05],
            "high": [92, 97, 105],
            "low": [88, 93, 95],
            "close": [91, 96, 100],
        })
        patterns = _detect_candlestick_patterns(df)
        names = [p["name"] for p in patterns]
        assert "doji" in names

    def test_hammer(self):
        """Hammer: small body at top, long lower shadow."""
        df = pd.DataFrame({
            "open": [100, 98, 100.2],
            "high": [102, 100, 100.3],
            "low": [98, 96, 94],
            "close": [99, 97, 100],
        })
        patterns = _detect_candlestick_patterns(df)
        names = [p["name"] for p in patterns]
        assert "hammer" in names

    def test_shooting_star(self):
        """Shooting star: small body at bottom, long upper shadow."""
        df = pd.DataFrame({
            "open": [100, 102, 100],
            "high": [102, 104, 108],
            "low": [98, 100, 100],
            "close": [101, 103, 100.5],
        })
        patterns = _detect_candlestick_patterns(df)
        names = [p["name"] for p in patterns]
        assert "shooting_star" in names

    def test_insufficient_data(self):
        """Less than 3 bars returns empty."""
        df = pd.DataFrame({
            "open": [100, 101],
            "high": [102, 103],
            "low": [98, 99],
            "close": [101, 102],
        })
        assert _detect_candlestick_patterns(df) == []

    def test_zero_range_bar(self):
        """Bar with zero range doesn't crash."""
        df = pd.DataFrame({
            "open": [100, 100, 100],
            "high": [100, 100, 100],
            "low": [100, 100, 100],
            "close": [100, 100, 100],
        })
        patterns = _detect_candlestick_patterns(df)
        assert isinstance(patterns, list)


class TestTrendStructure:
    def test_uptrend_detection(self):
        """Steady uptrend with zigzag should detect uptrend."""
        n = 25
        # Zigzag uptrend: rises 3, dips 1, rises 3, dips 1...
        base = 100 + np.arange(n) * 1.5
        zigzag = np.array([3, 1, -2, 3, 1, -2] * 5)[:n]
        prices = base + zigzag.astype(float)
        df = pd.DataFrame({
            "high": prices + 2,
            "low": prices - 2,
            "close": prices,
        })
        result = _detect_trend_structure(df)
        assert result["trend"] == "uptrend"
        assert result["strength"] >= 1

    def test_downtrend_detection(self):
        """Steady downtrend with zigzag should detect downtrend."""
        n = 25
        base = 200 - np.arange(n) * 1.5
        zigzag = np.array([-3, -1, 2, -3, -1, 2] * 5)[:n]
        prices = base + zigzag.astype(float)
        df = pd.DataFrame({
            "high": prices + 2,
            "low": prices - 2,
            "close": prices,
        })
        result = _detect_trend_structure(df)
        assert result["trend"] == "downtrend"
        assert result["strength"] >= 1

    def test_ranging_market(self):
        """Flat/ranging market should detect ranging."""
        n = 25
        np.random.seed(99)
        prices = 100 + np.random.randn(n) * 0.5  # Tight range
        df = pd.DataFrame({
            "high": prices + 0.5,
            "low": prices - 0.5,
            "close": prices,
        })
        result = _detect_trend_structure(df)
        assert result["trend"] == "ranging"

    def test_insufficient_data(self):
        """Less than lookback returns ranging."""
        df = pd.DataFrame({
            "high": [101, 102],
            "low": [99, 100],
            "close": [100, 101],
        })
        result = _detect_trend_structure(df)
        assert result["trend"] == "ranging"
        assert result["strength"] == 0


class TestTrendline:
    def test_upward_trend(self):
        """Steady uptrend has positive slope."""
        n = 25
        prices = 100 + np.arange(n) * 1.5
        df = pd.DataFrame({"close": prices, "atr": np.ones(n) * 2.0})
        result = _detect_trendline(df)
        assert result["direction"] == "up"
        assert result["r_squared"] > 0.9

    def test_downward_trend(self):
        """Steady downtrend has negative slope."""
        n = 25
        prices = 200 - np.arange(n) * 1.5
        df = pd.DataFrame({"close": prices, "atr": np.ones(n) * 2.0})
        result = _detect_trendline(df)
        assert result["direction"] == "down"
        assert result["r_squared"] > 0.9

    def test_flat_market(self):
        """Flat market has flat direction."""
        n = 25
        prices = np.ones(n) * 100
        df = pd.DataFrame({"close": prices, "atr": np.ones(n) * 2.0})
        result = _detect_trendline(df)
        assert result["direction"] == "flat"

    def test_insufficient_data(self):
        """Less than lookback returns defaults."""
        df = pd.DataFrame({"close": [100, 101], "atr": [1.0, 1.0]})
        result = _detect_trendline(df)
        assert result["direction"] == "flat"
        assert result["r_squared"] == 0.0


class TestChartPatterns:
    def test_double_top_confirmed(self):
        """Double top confirmed: two peaks at similar level, price breaks below valley."""
        prices = np.array([
            100, 102, 104, 106, 108, 110,  # rise
            108, 106, 104, 102, 100,        # drop to valley at 100
            102, 104, 106, 108, 110,        # rise to peak2 at 110
            108, 106, 104, 102, 100,        # drop
            98, 97, 96, 95, 94,             # break below valley
            93, 92, 91, 90, 89,
        ])
        df = pd.DataFrame({
            "open": prices, "high": prices + 1,
            "low": prices - 1, "close": prices,
        })
        patterns = _detect_chart_patterns(df)
        dt = next(p for p in patterns if p["name"] == "double_top")
        assert dt["type"] == "bearish"
        assert dt["status"] == "confirmed"
        assert dt["strength"] == 2
        assert "levels" in dt

    def test_double_top_forming(self):
        """Double top forming: two peaks at similar level, price still above valley."""
        prices = np.array([
            100, 102, 104, 106, 108, 110,  # rise to peak1
            108, 106, 104, 102, 100,        # drop to valley at 100
            102, 104, 106, 108, 110,        # rise to peak2 at 110
            108, 107, 106, 105, 104,        # slight drop, still above valley
            103, 103, 102, 102, 101,
            101, 101, 101, 101, 101,
        ])
        df = pd.DataFrame({
            "open": prices, "high": prices + 1,
            "low": prices - 1, "close": prices,
        })
        patterns = _detect_chart_patterns(df)
        dt = next((p for p in patterns if p["name"] == "double_top"), None)
        assert dt is not None
        assert dt["status"] == "forming"
        assert dt["strength"] == 1

    def test_double_bottom_confirmed(self):
        """Double bottom confirmed: two troughs at similar level, price breaks above peak."""
        prices = np.array([
            110, 108, 106, 104, 102, 100,  # drop
            102, 104, 106, 108, 110,        # rise to peak at 110
            108, 106, 104, 102, 100,        # drop to trough2 at 100
            102, 104, 106, 108, 110,        # rise
            112, 113, 114, 115, 116,        # break above peak
            117, 118, 119, 120, 121,
        ])
        df = pd.DataFrame({
            "open": prices, "high": prices + 1,
            "low": prices - 1, "close": prices,
        })
        patterns = _detect_chart_patterns(df)
        db = next(p for p in patterns if p["name"] == "double_bottom")
        assert db["type"] == "bullish"
        assert db["status"] == "confirmed"
        assert db["strength"] == 2

    def test_head_and_shoulders_confirmed(self):
        """H&S confirmed: three peaks, middle highest, price below neckline."""
        prices = np.array([
            100, 103, 106, 109, 112,  # rise to shoulder1 ~112
            109, 106, 103, 100,       # drop to valley1 ~100
            103, 106, 109, 112, 115, 118,  # rise to head ~118
            115, 112, 109, 106, 103, 100,  # drop to valley2 ~100
            103, 106, 109, 112,       # rise to shoulder2 ~112
            109, 106, 103, 100,       # drop
            97, 95, 93,               # break below neckline
        ])
        df = pd.DataFrame({
            "open": prices, "high": prices + 1,
            "low": prices - 1, "close": prices,
        })
        patterns = _detect_chart_patterns(df)
        hs = next(p for p in patterns if p["name"] == "head_and_shoulders")
        assert hs["type"] == "bearish"
        assert hs["status"] == "confirmed"
        assert hs["strength"] == 2

    def test_head_and_shoulders_forming(self):
        """H&S forming: structure present but price still above neckline."""
        prices = np.array([
            100, 103, 106, 109, 112,  # rise to shoulder1 ~112
            109, 106, 103, 100,       # drop to valley1 ~100
            103, 106, 109, 112, 115, 118,  # rise to head ~118
            115, 112, 109, 106, 103, 100,  # drop to valley2 ~100
            103, 106, 109, 112,       # rise to shoulder2 ~112
            110, 108, 106, 104,       # slight drop
            103, 102, 101,            # still above neckline (~100)
        ])
        df = pd.DataFrame({
            "open": prices, "high": prices + 1,
            "low": prices - 1, "close": prices,
        })
        patterns = _detect_chart_patterns(df)
        hs = next((p for p in patterns if p["name"] == "head_and_shoulders"), None)
        assert hs is not None
        assert hs["status"] == "forming"
        assert hs["strength"] == 1

    def test_inverse_head_and_shoulders_confirmed(self):
        """Inverse H&S confirmed: three troughs, middle lowest, price above neckline."""
        prices = np.array([
            120, 117, 114, 111, 108,  # drop to shoulder1 ~108
            111, 114, 117, 120,       # rise to peak1 ~120
            117, 114, 111, 108, 105, 102,  # drop to head ~102
            105, 108, 111, 114, 117, 120,  # rise to peak2 ~120
            117, 114, 111, 108,       # drop to shoulder2 ~108
            111, 114, 117, 120,       # rise
            123, 125, 127,            # break above neckline
        ])
        df = pd.DataFrame({
            "open": prices, "high": prices + 1,
            "low": prices - 1, "close": prices,
        })
        patterns = _detect_chart_patterns(df)
        ihs = next(p for p in patterns if p["name"] == "inverse_head_and_shoulders")
        assert ihs["type"] == "bullish"
        assert ihs["status"] == "confirmed"

    def test_bull_flag(self):
        """Bull flag: strong up-move then mild consolidation."""
        n = 30
        # Pole: strong up-move in first 12 bars
        pole = 100 + np.arange(12) * 5.0  # 100 → 155
        # Flag: gentle drift down in last 18 bars
        flag = 155 - np.arange(18) * 0.5 + np.sin(np.arange(18)) * 1
        prices = np.concatenate([pole, flag])
        df = pd.DataFrame({
            "open": prices,
            "high": prices + 2,
            "low": prices - 2,
            "close": prices,
        })
        patterns = _detect_chart_patterns(df)
        names = [p["name"] for p in patterns]
        assert "bull_flag" in names

    def test_bear_flag(self):
        """Bear flag: strong down-move then mild consolidation."""
        n = 30
        pole = 200 - np.arange(12) * 5.0  # 200 → 145
        flag = 145 + np.arange(18) * 0.5 + np.sin(np.arange(18)) * 1
        prices = np.concatenate([pole, flag])
        df = pd.DataFrame({
            "open": prices,
            "high": prices + 2,
            "low": prices - 2,
            "close": prices,
        })
        patterns = _detect_chart_patterns(df)
        names = [p["name"] for p in patterns]
        assert "bear_flag" in names

    def test_insufficient_data_returns_empty(self):
        """Less than 20 bars returns no patterns."""
        df = pd.DataFrame({
            "open": np.ones(10) * 100,
            "high": np.ones(10) * 101,
            "low": np.ones(10) * 99,
            "close": np.ones(10) * 100,
        })
        assert _detect_chart_patterns(df) == []

    def test_no_false_patterns_on_random(self):
        """Random walk shouldn't produce excessive pattern detections."""
        df = _make_ohlcv(n=60, seed=123)
        patterns = _detect_chart_patterns(df)
        # Some patterns might be detected but shouldn't be overwhelming
        assert len(patterns) <= 5


class TestDetectPatterns:
    def test_returns_all_sections(self):
        """Full detect_patterns returns all expected keys."""
        df = _make_ohlcv(n=50)
        result = detect_patterns(df)
        assert "candlestick_patterns" in result
        assert "chart_patterns" in result
        assert "trend_structure" in result
        assert "trendline" in result
        assert "enhanced_chart_score" in result

    def test_score_range(self):
        """Enhanced chart score should be within -2..+2."""
        df = _make_ohlcv(n=50)
        result = detect_patterns(df)
        assert -2.0 <= result["enhanced_chart_score"] <= 2.0

    def test_with_atr_column(self):
        """Should use ATR column for normalization if available."""
        df = _make_ohlcv(n=50)
        df["atr"] = 2.0
        result = detect_patterns(df)
        assert "trendline" in result
        assert isinstance(result["trendline"]["distance"], float)
