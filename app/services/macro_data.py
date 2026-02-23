"""Historical macro data service for multi-factor backtesting."""

import logging
import time

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# Which macro tickers are relevant for each instrument, and their correlation direction
INSTRUMENT_MACRO_MAP: dict[str, dict[str, str]] = {
    "XAUUSD": {"DX-Y.NYB": "inverse", "^TNX": "inverse", "yield_curve": "positive", "SI=F": "positive", "^GSPC": "neutral"},
    "MES": {"^VIX": "inverse", "^TNX": "neutral", "yield_curve": "positive", "^GSPC": "positive", "DX-Y.NYB": "neutral"},
    "IBUS500": {"^VIX": "inverse", "^TNX": "neutral", "yield_curve": "positive", "^GSPC": "positive", "DX-Y.NYB": "neutral"},
    "EURUSD": {"DX-Y.NYB": "inverse", "^TNX": "neutral", "yield_curve": "inverse"},
    "EURJPY": {"^VIX": "inverse", "^GSPC": "positive", "yield_curve": "positive"},
    "USDJPY": {"DX-Y.NYB": "positive", "^TNX": "positive", "yield_curve": "positive", "^VIX": "inverse"},
    "CADJPY": {"CL=F": "positive", "^VIX": "inverse", "yield_curve": "positive", "^GSPC": "positive"},
    "BTC": {"DX-Y.NYB": "inverse", "^VIX": "inverse", "yield_curve": "positive", "^GSPC": "positive"},
}

MACRO_TICKERS = ["DX-Y.NYB", "^VIX", "^TNX", "SI=F", "^GSPC", "^IRX", "CL=F"]

VIX_LEVELS = {
    "low": (0, 15),
    "normal": (15, 25),
    "high": (25, 35),
    "extreme": (35, float("inf")),
}


class MacroDataService:
    """Fetches historical macro data (DXY, VIX, yields, silver, S&P) from yfinance."""

    def __init__(self):
        self._cache: dict[str, tuple[pd.DataFrame, float]] = {}
        self._cache_ttl = 3600  # 1 hour

    def get_macro_data(self, instrument_key: str, lookback_days: int = 30) -> dict:
        """Get current macro snapshot for an instrument."""
        relevance = INSTRUMENT_MACRO_MAP.get(instrument_key.upper(), {})
        period = f"{lookback_days}d"

        try:
            data = self._fetch_batch(period)
        except Exception as e:
            logger.warning("Macro data fetch failed: %s", e)
            return {}

        result = {}

        if "DX-Y.NYB" in data.columns:
            dxy_series = data["DX-Y.NYB"].dropna()
            if len(dxy_series) >= 5:
                result["dxy"] = {
                    "close": round(float(dxy_series.iloc[-1]), 2),
                    "trend": "up" if dxy_series.iloc[-1] > dxy_series.iloc[-5] else "down",
                    "change_5d": round(float(dxy_series.iloc[-1] - dxy_series.iloc[-5]), 4),
                    "correlation": relevance.get("DX-Y.NYB", "neutral"),
                }

        if "^VIX" in data.columns:
            vix_series = data["^VIX"].dropna()
            if len(vix_series) >= 1:
                vix_val = float(vix_series.iloc[-1])
                level = "normal"
                for lbl, (lo, hi) in VIX_LEVELS.items():
                    if lo <= vix_val < hi:
                        level = lbl
                        break
                result["vix"] = {
                    "close": round(vix_val, 2),
                    "level": level,
                    "change_5d": round(float(vix_series.iloc[-1] - vix_series.iloc[-5]), 4) if len(vix_series) >= 5 else 0,
                    "correlation": relevance.get("^VIX", "neutral"),
                }

        if "^TNX" in data.columns:
            tnx_series = data["^TNX"].dropna()
            if len(tnx_series) >= 5:
                result["us10y"] = {
                    "close": round(float(tnx_series.iloc[-1]), 4),
                    "change_5d": round(float(tnx_series.iloc[-1] - tnx_series.iloc[-5]), 4),
                    "correlation": relevance.get("^TNX", "neutral"),
                }

        if "SI=F" in data.columns:
            silver_series = data["SI=F"].dropna()
            if len(silver_series) >= 1:
                result["silver"] = {
                    "close": round(float(silver_series.iloc[-1]), 2),
                    "correlation": relevance.get("SI=F", "neutral"),
                }

        if "^GSPC" in data.columns:
            sp_series = data["^GSPC"].dropna()
            if len(sp_series) >= 1:
                result["sp500"] = {
                    "close": round(float(sp_series.iloc[-1]), 2),
                    "correlation": relevance.get("^GSPC", "neutral"),
                }

        if "CL=F" in data.columns:
            cl_series = data["CL=F"].dropna()
            if len(cl_series) >= 5:
                result["crude_oil"] = {
                    "close": round(float(cl_series.iloc[-1]), 2),
                    "change_5d": round(float(cl_series.iloc[-1] - cl_series.iloc[-5]), 4),
                    "trend": "up" if cl_series.iloc[-1] > cl_series.iloc[-5] else "down",
                    "correlation": relevance.get("CL=F", "neutral"),
                }

        if "^TNX" in data.columns and "^IRX" in data.columns:
            tnx_series = data["^TNX"].dropna()
            irx_series = data["^IRX"].dropna()
            if len(tnx_series) >= 5 and len(irx_series) >= 5:
                yc_series = tnx_series - irx_series
                result["yield_curve"] = {
                    "spread": round(float(yc_series.iloc[-1]), 4),
                    "change_5d": round(float(yc_series.iloc[-1] - yc_series.iloc[-5]), 4),
                    "trend": "steepening" if yc_series.iloc[-1] > yc_series.iloc[-5] else "flattening",
                    "correlation": relevance.get("yield_curve", "neutral"),
                }

        return result

    def get_macro_series(self, instrument_key: str, period: str) -> pd.DataFrame:
        """Get daily macro time series aligned to trading dates for backtesting.

        Returns DataFrame with columns for each relevant macro ticker's close price.
        Index is DatetimeIndex aligned with trading dates.
        """
        try:
            data = self._fetch_batch(period)
        except Exception as e:
            logger.warning("Macro series fetch failed: %s", e)
            return pd.DataFrame()

        relevance = INSTRUMENT_MACRO_MAP.get(instrument_key.upper(), {})
        relevant_tickers = list(relevance.keys())

        # If yield_curve is needed, ensure underlying tickers are fetched
        needs_yield_curve = "yield_curve" in relevant_tickers
        if needs_yield_curve:
            relevant_tickers = [t for t in relevant_tickers if t != "yield_curve"]

        # Filter to relevant columns
        available = [t for t in relevant_tickers if t in data.columns]

        # Compute synthetic yield_curve from ^TNX - ^IRX
        if needs_yield_curve and "^TNX" in data.columns and "^IRX" in data.columns:
            data = data.copy()
            data["yield_curve"] = data["^TNX"] - data["^IRX"]
            if "yield_curve" not in available:
                available.append("yield_curve")

        if not available:
            return pd.DataFrame()

        result = data[available].copy()

        # Add 5-day change columns for trend scoring
        for col in available:
            result[f"{col}_change5"] = result[col].diff(5)
            result[f"{col}_trend"] = (result[col] > result[col].shift(5)).astype(float)

        # Forward-fill missing data (weekends/holidays)
        result = result.ffill()

        return result

    def get_instrument_correlations(self, instrument_key: str) -> dict[str, str]:
        """Return the correlation mapping for an instrument."""
        return INSTRUMENT_MACRO_MAP.get(instrument_key.upper(), {})

    def _fetch_batch(self, period: str) -> pd.DataFrame:
        """Fetch all macro tickers in a single batch download with caching."""
        now = time.monotonic()
        cache_key = period

        cached = self._cache.get(cache_key)
        if cached:
            df, ts = cached
            if now - ts < self._cache_ttl:
                return df

        logger.info("Fetching macro data for period=%s", period)
        raw = yf.download(
            MACRO_TICKERS,
            period=period,
            interval="1d",
            group_by="ticker",
            progress=False,
            threads=True,
        )

        if raw is None or raw.empty:
            return pd.DataFrame()

        # Extract close prices for each ticker
        close_data = {}
        for ticker in MACRO_TICKERS:
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    if ticker in raw.columns.get_level_values(0):
                        series = raw[ticker]["Close"]
                        close_data[ticker] = series
                else:
                    # Single ticker case
                    if "Close" in raw.columns:
                        close_data[MACRO_TICKERS[0]] = raw["Close"]
            except (KeyError, TypeError):
                logger.debug("Ticker %s not available in batch download", ticker)

        df = pd.DataFrame(close_data)
        self._cache[cache_key] = (df, now)
        return df
