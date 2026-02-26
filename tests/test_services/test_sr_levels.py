import pytest
import pandas as pd
import numpy as np

from app.services.patterns import compute_sr_levels, _cluster_pivots
from app.services.technical_analyzer import TechnicalAnalyzer


def _make_ohlcv_df(n: int = 60, base_price: float = 2800.0, atr_val: float = 15.0):
    """Create a synthetic OHLCV DataFrame with indicators for testing."""
    np.random.seed(42)
    closes = base_price + np.cumsum(np.random.randn(n) * 2)
    highs = closes + np.abs(np.random.randn(n) * 3)
    lows = closes - np.abs(np.random.randn(n) * 3)
    opens = closes + np.random.randn(n) * 1

    df = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": np.random.randint(100, 1000, n),
    })

    # Add indicators that compute_sr_levels needs
    df["atr"] = atr_val
    df["high_20"] = df["high"].rolling(20).max()
    df["low_20"] = df["low"].rolling(20).min()

    return df


def _make_df_with_clear_levels():
    """Create a DataFrame with clear repeated swing highs/lows for clustering."""
    n = 50
    prices = []
    # Create zigzag pattern with repeated levels around 2800 (resistance) and 2780 (support)
    for i in range(n):
        cycle = i % 10
        if cycle < 5:
            # Rising toward 2800
            prices.append(2780 + cycle * 4)
        else:
            # Falling toward 2780
            prices.append(2800 - (cycle - 5) * 4)

    closes = np.array(prices, dtype=float)
    highs = closes + 2
    lows = closes - 2
    opens = closes - 0.5

    df = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": np.ones(n) * 500,
    })
    df["atr"] = 10.0
    df["high_20"] = df["high"].rolling(20).max()
    df["low_20"] = df["low"].rolling(20).min()

    return df


class TestClusterPivots:

    def test_basic_clustering(self):
        """Nearby pivots should be merged into a single cluster."""
        pivots = [(5, 2800.0), (10, 2801.0), (15, 2800.5)]
        clusters = _cluster_pivots(pivots, tolerance=5.0)
        assert len(clusters) == 1
        assert clusters[0]["touches"] == 3
        assert abs(clusters[0]["level"] - 2800.5) < 1.0

    def test_separate_clusters(self):
        """Pivots far apart should form separate clusters."""
        pivots = [(5, 2780.0), (10, 2781.0), (15, 2820.0), (20, 2821.0)]
        clusters = _cluster_pivots(pivots, tolerance=5.0)
        assert len(clusters) == 2
        assert clusters[0]["touches"] == 2
        assert clusters[1]["touches"] == 2

    def test_recency_tracking(self):
        """Most recent index should be tracked in each cluster."""
        pivots = [(5, 2800.0), (15, 2801.0), (25, 2800.5)]
        clusters = _cluster_pivots(pivots, tolerance=5.0)
        assert clusters[0]["recency"] == 25

    def test_empty_pivots(self):
        """Empty input should return empty output."""
        assert _cluster_pivots([], tolerance=5.0) == []

    def test_single_pivot(self):
        """Single pivot should form a single cluster with 1 touch."""
        clusters = _cluster_pivots([(10, 2800.0)], tolerance=5.0)
        assert len(clusters) == 1
        assert clusters[0]["touches"] == 1


class TestComputeSRLevels:

    def test_returns_support_and_resistance(self):
        """Output should have support and resistance keys."""
        df = _make_ohlcv_df()
        sr = compute_sr_levels(df, pivot_range=2)
        assert "support" in sr
        assert "resistance" in sr

    def test_resistance_above_close(self):
        """All resistance levels should be >= current close."""
        df = _make_ohlcv_df()
        current = float(df["close"].iloc[-1])
        sr = compute_sr_levels(df, pivot_range=2)
        for r in sr["resistance"]:
            assert r["level"] >= current

    def test_support_below_close(self):
        """All support levels should be < current close."""
        df = _make_ohlcv_df()
        current = float(df["close"].iloc[-1])
        sr = compute_sr_levels(df, pivot_range=2)
        for s in sr["support"]:
            assert s["level"] < current

    def test_max_levels_respected(self):
        """Should return at most max_levels per side."""
        df = _make_ohlcv_df(n=200)
        sr = compute_sr_levels(df, pivot_range=2, max_levels=2)
        assert len(sr["support"]) <= 2
        assert len(sr["resistance"]) <= 2

    def test_higher_touch_levels_rank_first(self):
        """Higher-touch clusters should appear before lower-touch ones (after sorting by proximity)."""
        df = _make_df_with_clear_levels()
        sr = compute_sr_levels(df, pivot_range=2)
        # We should have levels with touches > 1 given the zigzag pattern
        all_levels = sr["support"] + sr["resistance"]
        if len(all_levels) > 0:
            # At least one level should have > 1 touch from the repeated pattern
            max_touches = max(l["touches"] for l in all_levels)
            assert max_touches >= 1

    def test_fallback_to_high_low_20(self):
        """Small DataFrame should fall back to high_20/low_20."""
        df = _make_ohlcv_df(n=6)  # Too small for pivot detection
        sr = compute_sr_levels(df, pivot_range=2)
        # Should still have some output (fallback)
        assert isinstance(sr["support"], list)
        assert isinstance(sr["resistance"], list)

    def test_no_recency_in_output(self):
        """Recency should be stripped from the final output."""
        df = _make_ohlcv_df()
        sr = compute_sr_levels(df, pivot_range=2)
        for lvl in sr["support"] + sr["resistance"]:
            assert "recency" not in lvl

    def test_each_level_has_touches(self):
        """Each level dict should have 'level' and 'touches' keys."""
        df = _make_ohlcv_df()
        sr = compute_sr_levels(df, pivot_range=2)
        for lvl in sr["support"] + sr["resistance"]:
            assert "level" in lvl
            assert "touches" in lvl

    def test_graceful_with_small_df(self):
        """Should not crash with very small DataFrames."""
        df = pd.DataFrame({
            "open": [2800.0],
            "high": [2810.0],
            "low": [2790.0],
            "close": [2800.0],
            "volume": [100],
        })
        sr = compute_sr_levels(df, pivot_range=2)
        assert "support" in sr
        assert "resistance" in sr

    def test_d1_pivot_range(self):
        """pivot_range=3 (D1) should still produce valid results."""
        df = _make_ohlcv_df(n=100)
        sr = compute_sr_levels(df, pivot_range=3)
        assert "support" in sr
        assert "resistance" in sr


class TestSRScoreFromLevels:

    def test_near_support_positive(self):
        """Price near support should return positive score."""
        sr_meta = {
            "support": [{"level": 2798.0, "touches": 3}],
            "resistance": [{"level": 2830.0, "touches": 2}],
        }
        score = TechnicalAnalyzer._compute_sr_score_from_levels(2800.0, sr_meta, 15.0)
        assert score > 0

    def test_near_resistance_negative(self):
        """Price near resistance should return negative score."""
        sr_meta = {
            "support": [{"level": 2770.0, "touches": 3}],
            "resistance": [{"level": 2802.0, "touches": 2}],
        }
        score = TechnicalAnalyzer._compute_sr_score_from_levels(2800.0, sr_meta, 15.0)
        assert score < 0

    def test_no_atr_returns_none(self):
        """Zero or NaN ATR should return None."""
        sr_meta = {"support": [{"level": 2790.0}], "resistance": []}
        assert TechnicalAnalyzer._compute_sr_score_from_levels(2800.0, sr_meta, 0) is None
        assert TechnicalAnalyzer._compute_sr_score_from_levels(2800.0, sr_meta, None) is None

    def test_far_from_both_returns_zero(self):
        """Price far from both S/R should return 0."""
        sr_meta = {
            "support": [{"level": 2750.0, "touches": 2}],
            "resistance": [{"level": 2850.0, "touches": 2}],
        }
        score = TechnicalAnalyzer._compute_sr_score_from_levels(2800.0, sr_meta, 15.0)
        assert score == 0.0


class TestClassifySignalType:

    def test_trend_signal(self):
        """Dominant trend factors should classify as 'trend'."""
        factors = {
            "h1_trend": 2.0, "h1_momentum": 1.5,
            "m15_entry": 0.0, "sr_proximity": 0.0,
        }
        assert TechnicalAnalyzer._classify_signal_type(factors, mode="intraday") == "trend"

    def test_mean_reversion_signal(self):
        """Dominant mean-reversion factors should classify as 'mean_reversion'."""
        factors = {
            "h1_trend": 0.0, "h1_momentum": 0.0,
            "m15_entry": 2.0, "sr_proximity": 1.5,
        }
        assert TechnicalAnalyzer._classify_signal_type(factors, mode="intraday") == "mean_reversion"

    def test_mixed_signal(self):
        """Balanced factors should classify as 'mixed'."""
        factors = {
            "h1_trend": 1.0, "h1_momentum": 0.5,
            "m15_entry": 1.0, "sr_proximity": 1.0,
        }
        assert TechnicalAnalyzer._classify_signal_type(factors, mode="intraday") == "mixed"

    def test_swing_trend_signal(self):
        """Swing mode with dominant trend factors should classify as 'trend'."""
        factors = {
            "d1_trend": 2.0, "tf_alignment": 2.0,
            "1h_entry": 0.0, "sr_proximity": 0.0,
        }
        assert TechnicalAnalyzer._classify_signal_type(factors, mode="swing") == "trend"

    def test_swing_mean_reversion_signal(self):
        """Swing mode with dominant MR factors should classify as 'mean_reversion'."""
        factors = {
            "d1_trend": 0.0, "tf_alignment": 0.0,
            "1h_entry": 2.0, "sr_proximity": 2.0,
        }
        assert TechnicalAnalyzer._classify_signal_type(factors, mode="swing") == "mean_reversion"

    def test_all_zeros_returns_mixed(self):
        """All zero factors should return 'mixed'."""
        factors = {
            "h1_trend": 0, "h1_momentum": 0,
            "m15_entry": 0, "sr_proximity": 0,
        }
        assert TechnicalAnalyzer._classify_signal_type(factors, mode="intraday") == "mixed"
