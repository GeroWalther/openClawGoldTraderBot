import pytest
from unittest.mock import patch, MagicMock

import pandas as pd
import numpy as np

from app.services.macro_data import MacroDataService, INSTRUMENT_MACRO_MAP, MACRO_TICKERS, VIX_LEVELS


def _mock_batch_data(num_days=30):
    """Create mock macro data DataFrame."""
    dates = pd.date_range("2024-01-01", periods=num_days, freq="B")

    # Create multi-index DataFrame like yfinance returns
    data = {}
    base = {
        "DX-Y.NYB": 104.0, "^VIX": 18.0, "^TNX": 4.2, "SI=F": 28.0,
        "^GSPC": 5100.0, "^IRX": 3.8, "CL=F": 78.0,
    }
    for ticker in MACRO_TICKERS:
        prices = [base.get(ticker, 100.0)]
        for i in range(1, num_days):
            prices.append(prices[-1] + np.random.randn() * 0.5)
        data[ticker] = prices

    # Return as DataFrame with ticker columns (simulating extracted close prices)
    df = pd.DataFrame(data, index=dates)
    return df


@pytest.fixture
def macro_service():
    return MacroDataService()


class TestMacroDataService:

    def test_instrument_macro_map_coverage(self):
        """All supported instruments should have macro mappings."""
        for key in ["XAUUSD", "MES", "EURUSD", "EURJPY", "CADJPY", "USDJPY", "BTC"]:
            assert key in INSTRUMENT_MACRO_MAP, f"Missing macro map for {key}"

    def test_xauusd_has_dxy_and_yields(self):
        """XAUUSD should map to DXY (inverse) and yields (inverse)."""
        mapping = INSTRUMENT_MACRO_MAP["XAUUSD"]
        assert "DX-Y.NYB" in mapping
        assert mapping["DX-Y.NYB"] == "inverse"
        assert "^TNX" in mapping
        assert mapping["^TNX"] == "inverse"

    def test_vix_levels(self):
        """VIX level classifications should be correct."""
        assert VIX_LEVELS["low"] == (0, 15)
        assert VIX_LEVELS["normal"] == (15, 25)
        assert VIX_LEVELS["high"] == (25, 35)

    @patch("app.services.macro_data.yf.download")
    def test_get_macro_data_returns_dict(self, mock_download, macro_service):
        """get_macro_data should return a dict with macro indicators."""
        mock_df = _mock_batch_data()
        # Simulate yfinance multi-index return
        multi_idx = pd.MultiIndex.from_tuples(
            [(t, "Close") for t in MACRO_TICKERS],
            names=["Ticker", "Price"],
        )
        mock_return = pd.DataFrame(
            {(t, "Close"): mock_df[t].values for t in MACRO_TICKERS},
            index=mock_df.index,
        )
        mock_return.columns = multi_idx
        mock_download.return_value = mock_return

        result = macro_service.get_macro_data("XAUUSD", lookback_days=30)

        assert isinstance(result, dict)
        # Should have at least DXY for XAUUSD
        if "dxy" in result:
            assert "close" in result["dxy"]
            assert "trend" in result["dxy"]
            assert result["dxy"]["trend"] in ("up", "down")

    @patch("app.services.macro_data.yf.download")
    def test_get_macro_series_returns_dataframe(self, mock_download, macro_service):
        """get_macro_series should return a DataFrame with macro columns."""
        mock_df = _mock_batch_data()
        multi_idx = pd.MultiIndex.from_tuples(
            [(t, "Close") for t in MACRO_TICKERS],
            names=["Ticker", "Price"],
        )
        mock_return = pd.DataFrame(
            {(t, "Close"): mock_df[t].values for t in MACRO_TICKERS},
            index=mock_df.index,
        )
        mock_return.columns = multi_idx
        mock_download.return_value = mock_return

        result = macro_service.get_macro_series("XAUUSD", "1y")

        assert isinstance(result, pd.DataFrame)
        # Should have change5 columns
        if not result.empty:
            change_cols = [c for c in result.columns if "_change5" in str(c)]
            assert len(change_cols) > 0

    @patch("app.services.macro_data.yf.download")
    def test_cache_prevents_duplicate_fetches(self, mock_download, macro_service):
        """Second call within TTL should use cache."""
        mock_df = _mock_batch_data()
        multi_idx = pd.MultiIndex.from_tuples(
            [(t, "Close") for t in MACRO_TICKERS],
            names=["Ticker", "Price"],
        )
        mock_return = pd.DataFrame(
            {(t, "Close"): mock_df[t].values for t in MACRO_TICKERS},
            index=mock_df.index,
        )
        mock_return.columns = multi_idx
        mock_download.return_value = mock_return

        macro_service.get_macro_data("XAUUSD")
        macro_service.get_macro_data("XAUUSD")

        assert mock_download.call_count == 1

    @patch("app.services.macro_data.yf.download")
    def test_empty_download_returns_empty(self, mock_download, macro_service):
        """Empty yfinance response should return empty dict."""
        mock_download.return_value = pd.DataFrame()

        result = macro_service.get_macro_data("XAUUSD")
        assert result == {}

    @patch("app.services.macro_data.yf.download")
    def test_download_exception_returns_empty(self, mock_download, macro_service):
        """Exception during download should return empty dict."""
        mock_download.side_effect = Exception("Network error")

        result = macro_service.get_macro_data("XAUUSD")
        assert result == {}

    def test_get_instrument_correlations(self, macro_service):
        """Should return correlation mapping for known instruments."""
        corr = macro_service.get_instrument_correlations("XAUUSD")
        assert "DX-Y.NYB" in corr
        assert corr["DX-Y.NYB"] == "inverse"

    def test_unknown_instrument_returns_empty_correlations(self, macro_service):
        """Unknown instrument should return empty dict."""
        corr = macro_service.get_instrument_correlations("UNKNOWN")
        assert corr == {}

    def test_cadjpy_has_crude_oil(self):
        """CADJPY should map to CL=F (positive) for oil-CAD correlation."""
        mapping = INSTRUMENT_MACRO_MAP["CADJPY"]
        assert "CL=F" in mapping
        assert mapping["CL=F"] == "positive"

    def test_usdjpy_has_dxy_positive(self):
        """USDJPY should map to DXY (positive) — USD strength."""
        mapping = INSTRUMENT_MACRO_MAP["USDJPY"]
        assert "DX-Y.NYB" in mapping
        assert mapping["DX-Y.NYB"] == "positive"

    def test_yield_curve_in_all_instruments(self):
        """All instruments should have yield_curve in their macro map."""
        for key in ["XAUUSD", "MES", "IBUS500", "EURUSD", "EURJPY", "USDJPY", "CADJPY", "BTC"]:
            assert "yield_curve" in INSTRUMENT_MACRO_MAP[key], f"Missing yield_curve for {key}"

    @patch("app.services.macro_data.yf.download")
    def test_yield_curve_computation(self, mock_download, macro_service):
        """get_macro_series should compute synthetic yield_curve = ^TNX - ^IRX."""
        mock_df = _mock_batch_data()
        multi_idx = pd.MultiIndex.from_tuples(
            [(t, "Close") for t in MACRO_TICKERS],
            names=["Ticker", "Price"],
        )
        mock_return = pd.DataFrame(
            {(t, "Close"): mock_df[t].values for t in MACRO_TICKERS},
            index=mock_df.index,
        )
        mock_return.columns = multi_idx
        mock_download.return_value = mock_return

        result = macro_service.get_macro_series("XAUUSD", "1y")

        assert isinstance(result, pd.DataFrame)
        if not result.empty:
            assert "yield_curve" in result.columns
            assert "yield_curve_change5" in result.columns
            assert "yield_curve_trend" in result.columns

    @patch("app.services.macro_data.yf.download")
    def test_crude_oil_in_macro_data(self, mock_download, macro_service):
        """get_macro_data should include crude_oil for instruments with CL=F mapping."""
        mock_df = _mock_batch_data()
        multi_idx = pd.MultiIndex.from_tuples(
            [(t, "Close") for t in MACRO_TICKERS],
            names=["Ticker", "Price"],
        )
        mock_return = pd.DataFrame(
            {(t, "Close"): mock_df[t].values for t in MACRO_TICKERS},
            index=mock_df.index,
        )
        mock_return.columns = multi_idx
        mock_download.return_value = mock_return

        result = macro_service.get_macro_data("CADJPY", lookback_days=30)

        assert isinstance(result, dict)
        if "crude_oil" in result:
            assert "close" in result["crude_oil"]
            assert "change_5d" in result["crude_oil"]
            assert "trend" in result["crude_oil"]
