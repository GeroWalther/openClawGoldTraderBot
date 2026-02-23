import time
import pytest
from unittest.mock import patch, MagicMock

import pandas as pd

from app.instruments import get_instrument
from app.services.atr_calculator import ATRCalculator


def _mock_history(num_bars=30, high_base=2950, low_base=2900, close_base=2920):
    """Create a simple DataFrame simulating daily OHLC data."""
    import numpy as np
    dates = pd.date_range("2024-01-01", periods=num_bars, freq="D")
    np.random.seed(42)
    df = pd.DataFrame({
        "Open": [close_base + np.random.randn() * 10 for _ in range(num_bars)],
        "High": [high_base + np.random.randn() * 5 for _ in range(num_bars)],
        "Low": [low_base + np.random.randn() * 5 for _ in range(num_bars)],
        "Close": [close_base + np.random.randn() * 10 for _ in range(num_bars)],
        "Volume": [1000] * num_bars,
    }, index=dates)
    return df


@pytest.fixture
def atr_calc(settings):
    return ATRCalculator(settings)


def test_get_dynamic_sl_tp_success(atr_calc):
    instrument = get_instrument("XAUUSD")
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = _mock_history()

    with patch("app.services.atr_calculator.yf.Ticker", return_value=mock_ticker):
        result = atr_calc.get_dynamic_sl_tp(instrument)

    assert result is not None
    sl, tp = result
    assert sl >= instrument.min_stop_distance
    assert sl <= instrument.max_stop_distance
    assert tp >= sl


def test_cache_returns_same_value(atr_calc):
    instrument = get_instrument("XAUUSD")
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = _mock_history()

    with patch("app.services.atr_calculator.yf.Ticker", return_value=mock_ticker):
        result1 = atr_calc.get_dynamic_sl_tp(instrument)
        result2 = atr_calc.get_dynamic_sl_tp(instrument)

    assert result1 == result2
    # Only called once due to cache
    assert mock_ticker.history.call_count == 1


def test_insufficient_data_returns_none(atr_calc):
    instrument = get_instrument("XAUUSD")
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = _mock_history(num_bars=5)

    with patch("app.services.atr_calculator.yf.Ticker", return_value=mock_ticker):
        result = atr_calc.get_dynamic_sl_tp(instrument)

    assert result is None


def test_fetch_failure_returns_none(atr_calc):
    instrument = get_instrument("XAUUSD")
    mock_ticker = MagicMock()
    mock_ticker.history.side_effect = Exception("Network error")

    with patch("app.services.atr_calculator.yf.Ticker", return_value=mock_ticker):
        result = atr_calc.get_dynamic_sl_tp(instrument)

    assert result is None


def test_disabled_returns_none(settings):
    settings.atr_enabled = False
    atr_calc = ATRCalculator(settings)
    instrument = get_instrument("XAUUSD")
    result = atr_calc.get_dynamic_sl_tp(instrument)
    assert result is None


def test_clamping_to_bounds(atr_calc):
    """ATR values that would exceed bounds get clamped."""
    instrument = get_instrument("XAUUSD")
    # Create data with very high volatility
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = _mock_history(high_base=3500, low_base=2500)

    with patch("app.services.atr_calculator.yf.Ticker", return_value=mock_ticker):
        result = atr_calc.get_dynamic_sl_tp(instrument)

    if result is not None:
        sl, tp = result
        assert sl <= instrument.max_stop_distance
        assert sl >= instrument.min_stop_distance
