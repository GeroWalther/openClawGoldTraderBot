"""Multi-timeframe technical analysis service.

Fetches OHLCV data from yfinance, computes indicators, scores with
ScoringEngine + MacroDataService, and returns a compact JSON response
suitable for consumption by OpenClaw skills (replacing Tavily searches).
"""

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

from app.instruments import INSTRUMENTS, InstrumentSpec
from app.services.indicators import compute_indicators
from app.services.macro_data import MacroDataService, VIX_LEVELS
from app.services.scoring_engine import ScoringEngine

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=4)


def _fetch_ohlcv(symbol: str, period: str, interval: str) -> pd.DataFrame:
    """Fetch OHLCV data from yfinance (blocking — run in executor)."""
    ticker = yf.Ticker(symbol)
    df = ticker.history(period=period, interval=interval)
    if df is None or df.empty:
        return pd.DataFrame()
    df.columns = [c.lower() for c in df.columns]
    return df


def _resample_to_4h(df_1h: pd.DataFrame) -> pd.DataFrame:
    """Resample 1H data to 4H bars."""
    if df_1h.empty:
        return pd.DataFrame()
    ohlcv = df_1h.resample("4h").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna(subset=["open"])
    return ohlcv


def _classify_trend(row: pd.Series) -> str:
    """Classify trend as bullish/bearish/neutral from SMA alignment."""
    close = row.get("close")
    sma20 = row.get("sma20")
    sma50 = row.get("sma50")
    sma200 = row.get("sma200")

    if any(pd.isna(v) for v in [close, sma20, sma50]):
        return "neutral"

    bullish_count = 0
    if close > sma20:
        bullish_count += 1
    if sma20 > sma50:
        bullish_count += 1
    if not pd.isna(sma200) and sma50 > sma200:
        bullish_count += 1

    if bullish_count >= 3 or (pd.isna(sma200) and bullish_count >= 2):
        return "bullish"
    elif bullish_count == 0:
        return "bearish"
    return "neutral"


def _sma_alignment(row: pd.Series) -> str | None:
    """Return SMA alignment string like '20>50>200'."""
    sma20 = row.get("sma20")
    sma50 = row.get("sma50")
    sma200 = row.get("sma200")

    if any(pd.isna(v) for v in [sma20, sma50]):
        return None
    if pd.isna(sma200):
        if sma20 > sma50:
            return "20>50"
        return "50>20"

    values = [(sma20, "20"), (sma50, "50"), (sma200, "200")]
    values.sort(key=lambda x: x[0], reverse=True)
    return ">".join(v[1] for v in values)


def _macd_crossover(row: pd.Series) -> str:
    macd = row.get("macd")
    signal = row.get("macd_signal")
    if pd.isna(macd) or pd.isna(signal):
        return "neutral"
    return "bullish" if macd > signal else "bearish"


def _session_info(instrument: InstrumentSpec) -> dict:
    """Get current session status for an instrument."""
    now = datetime.now(timezone.utc)
    current_hour = now.hour

    if not instrument.trading_sessions:
        return {"active": True, "current": "24/7"}

    for session in instrument.trading_sessions:
        start, end = session.start_hour_utc, session.end_hour_utc
        if start <= end:
            in_range = start <= current_hour < end
        else:
            in_range = current_hour >= start or current_hour < end
        if in_range:
            return {"active": True, "current": session.name}

    return {"active": False, "current": "closed"}


def _build_timeframe_block(df: pd.DataFrame, include_sma: bool = False) -> dict | None:
    """Build a compact analysis block from an indicator DataFrame."""
    if df.empty:
        return None
    row = df.iloc[-1]
    result = {"trend": _classify_trend(row)}

    if include_sma:
        alignment = _sma_alignment(row)
        if alignment:
            result["sma_alignment"] = alignment

    rsi = row.get("rsi")
    if not pd.isna(rsi):
        result["rsi"] = round(float(rsi), 1)

    result["macd"] = {"crossover": _macd_crossover(row)}

    hist = row.get("macd_hist")
    if not pd.isna(hist):
        result["macd"]["histogram"] = "growing" if hist > 0 else "shrinking"

    if include_sma:
        atr = row.get("atr")
        if not pd.isna(atr):
            result["atr"] = round(float(atr), 2)

        bb_upper = row.get("bb_upper")
        bb_lower = row.get("bb_lower")
        bb_bandwidth = row.get("bb_bandwidth")
        if not pd.isna(bb_bandwidth):
            result["bollinger"] = {
                "bandwidth": round(float(bb_bandwidth), 4),
                "squeeze": bool(bb_bandwidth < 0.02),
            }

    return result


class TechnicalAnalyzer:
    """Runs multi-timeframe technical analysis and scoring for all instruments."""

    def __init__(self):
        self._cache: dict[str, tuple[dict, float]] = {}
        self._cache_ttl = 300  # 5 minutes
        self._macro_service = MacroDataService()
        self._scoring_engine = ScoringEngine()

    async def analyze(self, instrument_key: str) -> dict:
        """Full multi-timeframe analysis for a single instrument."""
        key = instrument_key.upper()
        now = time.monotonic()

        cached = self._cache.get(key)
        if cached:
            data, ts = cached
            if now - ts < self._cache_ttl:
                return data

        if key not in INSTRUMENTS:
            return {"error": f"Unknown instrument: {key}", "available": list(INSTRUMENTS)}

        instrument = INSTRUMENTS[key]
        result = await self._run_analysis(key, instrument)
        self._cache[key] = (result, time.monotonic())
        return result

    async def scan_all(self) -> dict:
        """Scan all instruments and return ranked results."""
        tasks = [self.analyze(key) for key in INSTRUMENTS]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        instruments = []
        for key, result in zip(INSTRUMENTS, results):
            if isinstance(result, Exception):
                logger.warning("Scan failed for %s: %s", key, result)
                instruments.append({
                    "instrument": key,
                    "error": str(result),
                })
            elif "error" in result:
                instruments.append({"instrument": key, "error": result["error"]})
            else:
                instruments.append(result)

        # Sort by absolute score descending (best opportunities first)
        def sort_key(item):
            scoring = item.get("scoring", {})
            score = scoring.get("total_score", 0)
            return abs(score) if score is not None else 0

        instruments.sort(key=sort_key, reverse=True)

        # Build macro summary
        macro_summary = {}
        for item in instruments:
            if "macro" in item:
                macro_summary = item["macro"]
                break

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "instrument_count": len(instruments),
            "macro": macro_summary,
            "instruments": instruments,
        }

    async def _run_analysis(self, key: str, instrument: InstrumentSpec) -> dict:
        """Execute analysis: fetch data, compute indicators, score."""
        warnings = []
        loop = asyncio.get_event_loop()
        symbol = instrument.yahoo_symbol

        # Fetch D1, H1 (for 4H resample), and H1-recent concurrently
        d1_future = loop.run_in_executor(_executor, _fetch_ohlcv, symbol, "1y", "1d")
        h1_month_future = loop.run_in_executor(_executor, _fetch_ohlcv, symbol, "1mo", "1h")
        h1_week_future = loop.run_in_executor(_executor, _fetch_ohlcv, symbol, "5d", "1h")

        results = await asyncio.gather(
            d1_future, h1_month_future, h1_week_future,
            return_exceptions=True,
        )

        d1_df = results[0] if not isinstance(results[0], Exception) else pd.DataFrame()
        h1_month_df = results[1] if not isinstance(results[1], Exception) else pd.DataFrame()
        h1_week_df = results[2] if not isinstance(results[2], Exception) else pd.DataFrame()

        if isinstance(results[0], Exception):
            warnings.append(f"D1 fetch failed: {results[0]}")
        if isinstance(results[1], Exception):
            warnings.append(f"H1 month fetch failed: {results[1]}")
        if isinstance(results[2], Exception):
            warnings.append(f"H1 week fetch failed: {results[2]}")

        if d1_df.empty:
            return {"error": f"D1 data unavailable for {key}", "warnings": warnings}

        # Compute indicators for each timeframe
        d1_block = None
        d1_row = None
        if not d1_df.empty and len(d1_df) >= 20:
            try:
                compute_indicators(d1_df)
                d1_block = _build_timeframe_block(d1_df, include_sma=True)
                d1_row = d1_df.iloc[-1]
            except Exception as e:
                warnings.append(f"D1 indicators failed: {e}")
        else:
            warnings.append("D1 data insufficient")

        h4_block = None
        if not h1_month_df.empty and len(h1_month_df) >= 20:
            try:
                h4_df = _resample_to_4h(h1_month_df)
                if len(h4_df) >= 14:
                    compute_indicators(h4_df)
                    h4_block = _build_timeframe_block(h4_df)
                else:
                    warnings.append("4H data insufficient after resample")
            except Exception as e:
                warnings.append(f"4H indicators failed: {e}")
        else:
            warnings.append("H1-month data insufficient for 4H resample")

        h1_block = None
        if not h1_week_df.empty and len(h1_week_df) >= 14:
            try:
                compute_indicators(h1_week_df)
                h1_block = _build_timeframe_block(h1_week_df)
            except Exception as e:
                warnings.append(f"H1 indicators failed: {e}")
        else:
            warnings.append("H1 data insufficient")

        # Current price info
        price_info = {}
        if d1_row is not None:
            current = float(d1_row["close"])
            prev_close = float(d1_df["close"].iloc[-2]) if len(d1_df) >= 2 else current
            change_pct = round((current - prev_close) / prev_close * 100, 2) if prev_close else 0
            price_info = {
                "current": round(current, 4 if current < 10 else 2),
                "previous_close": round(prev_close, 4 if prev_close < 10 else 2),
                "change_pct": change_pct,
            }

        # Support/Resistance levels from D1 data
        levels = {}
        if d1_row is not None:
            high_20 = d1_row.get("high_20")
            low_20 = d1_row.get("low_20")
            close = d1_row["close"]
            atr = d1_row.get("atr")
            if not pd.isna(high_20) and not pd.isna(atr):
                levels["resistance"] = [
                    round(float(high_20), 2),
                    round(float(high_20 + atr), 2),
                ]
            if not pd.isna(low_20) and not pd.isna(atr):
                levels["support"] = [
                    round(float(low_20), 2),
                    round(float(low_20 - atr), 2),
                ]

        # Macro data
        macro_data = {}
        try:
            macro_data = self._macro_service.get_macro_data(key)
        except Exception as e:
            warnings.append(f"Macro data fetch failed: {e}")

        # Scoring (uses D1 row + macro series)
        scoring = {}
        if d1_row is not None:
            try:
                macro_series = self._macro_service.get_macro_series(key, "1y")
                macro_row = None
                if not macro_series.empty:
                    d1_date = d1_row.name
                    if hasattr(d1_date, "date"):
                        d1_date = d1_date.tz_localize(None) if d1_date.tzinfo else d1_date
                    macro_series.index = macro_series.index.tz_localize(None) if macro_series.index.tzinfo else macro_series.index
                    # Find closest macro row by date
                    idx = macro_series.index.get_indexer([d1_date], method="ffill")
                    if idx[0] >= 0:
                        macro_row = macro_series.iloc[idx[0]]

                score_result = self._scoring_engine.score_bar(d1_row, macro_row, key)
                scoring = {
                    "total_score": score_result["total_score"],
                    "max_score": 28,
                    "direction": score_result["direction"],
                    "conviction": score_result["conviction"],
                    "factors": score_result["factors"],
                }
            except Exception as e:
                warnings.append(f"Scoring failed: {e}")

        # Session info
        session = _session_info(instrument)

        # Build summary
        direction = scoring.get("direction", "NO TRADE")
        conviction = scoring.get("conviction")
        total = scoring.get("total_score", 0)
        d1_trend = d1_block.get("trend", "?") if d1_block else "?"
        rsi_val = d1_block.get("rsi", "?") if d1_block else "?"
        summary = (
            f"{conviction or 'LOW'} conviction {direction or 'NO TRADE'}. "
            f"D1 trend {d1_trend}. RSI {rsi_val}. "
            f"Score {total}/{scoring.get('max_score', 28)}."
        )

        result = {
            "instrument": key,
            "display_name": instrument.display_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "price": price_info,
            "technicals": {},
            "levels": levels,
            "macro": macro_data,
            "scoring": scoring,
            "session": session,
            "summary": summary,
        }

        if d1_block:
            result["technicals"]["d1"] = d1_block
        if h4_block:
            result["technicals"]["h4"] = h4_block
        if h1_block:
            result["technicals"]["h1"] = h1_block

        if warnings:
            result["warnings"] = warnings

        return result
