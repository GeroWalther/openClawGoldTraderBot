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
from app.services.calendar import CalendarService
from app.services.indicators import compute_indicators, compute_scalp_indicators
from app.services.macro_data import MacroDataService, VIX_LEVELS
from app.services.news import NewsService
from app.services.patterns import compute_sr_levels, detect_patterns
from app.services.intraday_scoring import (
    INTRADAY_FACTOR_WEIGHTS,
    INTRADAY_HIGH_CONVICTION_THRESHOLD,
    INTRADAY_SIGNAL_THRESHOLD,
    IntradayScoringEngine,
)
from app.services.m5_scalp_scoring import (
    M5_FACTOR_WEIGHTS,
    M5_HIGH_CONVICTION_THRESHOLD,
    M5_SIGNAL_THRESHOLD,
    M5ScalpScoringEngine,
)
from app.services.scoring_engine import (
    FACTOR_WEIGHTS,
    HIGH_CONVICTION_THRESHOLD,
    SIGNAL_THRESHOLD,
    ScoringEngine,
)

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
        self._intraday_cache_ttl = 120  # 2 minutes for intraday
        self._macro_service = MacroDataService()
        self._scoring_engine = ScoringEngine()
        self._intraday_scoring_engine = IntradayScoringEngine()
        self._m5_scalp_scoring_engine = M5ScalpScoringEngine()
        self._calendar_service = CalendarService()
        self._news_service = NewsService()

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

    async def analyze_intraday(self, instrument_key: str) -> dict:
        """Intraday/scalp analysis using 1H and 15m timeframes."""
        key = instrument_key.upper()
        cache_key = f"{key}_intraday"
        now = time.monotonic()

        cached = self._cache.get(cache_key)
        if cached:
            data, ts = cached
            if now - ts < self._intraday_cache_ttl:
                return data

        if key not in INSTRUMENTS:
            return {"error": f"Unknown instrument: {key}", "available": list(INSTRUMENTS)}

        instrument = INSTRUMENTS[key]
        result = await self._run_intraday_analysis(key, instrument)
        self._cache[cache_key] = (result, time.monotonic())
        return result

    async def analyze_m5_scalp(self, instrument_key: str) -> dict:
        """M5 scalp analysis using H1 trend gate and M5 entry signals."""
        key = instrument_key.upper()
        cache_key = f"{key}_m5scalp"
        now = time.monotonic()

        cached = self._cache.get(cache_key)
        if cached:
            data, ts = cached
            if now - ts < 60:  # 1-minute cache for M5 scalps
                return data

        if key not in INSTRUMENTS:
            return {"error": f"Unknown instrument: {key}", "available": list(INSTRUMENTS)}

        instrument = INSTRUMENTS[key]
        result = await self._run_m5_scalp_analysis(key, instrument)
        self._cache[cache_key] = (result, time.monotonic())
        return result

    async def _run_m5_scalp_analysis(self, key: str, instrument: InstrumentSpec) -> dict:
        """Execute M5 scalp analysis: fetch H1 + M5 data, score."""
        warnings = []
        loop = asyncio.get_event_loop()
        symbol = instrument.yahoo_symbol

        # Fetch H1 (1 month) and M5 (5 days) concurrently
        h1_future = loop.run_in_executor(_executor, _fetch_ohlcv, symbol, "1mo", "1h")
        m5_future = loop.run_in_executor(_executor, _fetch_ohlcv, symbol, "5d", "5m")

        results = await asyncio.gather(h1_future, m5_future, return_exceptions=True)

        h1_df = results[0] if not isinstance(results[0], Exception) else pd.DataFrame()
        m5_df = results[1] if not isinstance(results[1], Exception) else pd.DataFrame()

        if isinstance(results[0], Exception):
            warnings.append(f"H1 fetch failed: {results[0]}")
        if isinstance(results[1], Exception):
            warnings.append(f"M5 fetch failed: {results[1]}")

        if h1_df.empty:
            return {"error": f"H1 data unavailable for {key}", "warnings": warnings}
        if m5_df.empty:
            return {"error": f"M5 data unavailable for {key}", "warnings": warnings}

        # Compute indicators on H1
        h1_row = None
        h1_block = None
        if len(h1_df) >= 20:
            try:
                compute_indicators(h1_df)
                h1_block = _build_timeframe_block(h1_df, include_sma=True)
                h1_row = h1_df.iloc[-1]
            except Exception as e:
                warnings.append(f"H1 indicators failed: {e}")

        # Compute indicators on M5 (standard + scalp)
        m5_block = None
        m5_tail = pd.DataFrame()
        if len(m5_df) >= 21:
            try:
                compute_indicators(m5_df)
                compute_scalp_indicators(m5_df)
                m5_block = _build_timeframe_block(m5_df, include_sma=True)
                m5_tail = m5_df.tail(6)  # Last 6 bars for cross detection
            except Exception as e:
                warnings.append(f"M5 indicators failed: {e}")

        if h1_row is None:
            return {"error": f"H1 indicators unavailable for {key}", "warnings": warnings}
        if m5_tail.empty:
            return {"error": f"M5 indicators unavailable for {key}", "warnings": warnings}

        # Current price from M5
        m5_last = m5_df.iloc[-1]
        current = float(m5_last["close"])
        prev_close = float(m5_df["close"].iloc[-2]) if len(m5_df) >= 2 else current
        change_pct = round((current - prev_close) / prev_close * 100, 2) if prev_close else 0
        price_info = {
            "current": round(current, 4 if current < 10 else 2),
            "previous_close": round(prev_close, 4 if prev_close < 10 else 2),
            "change_pct": change_pct,
        }

        # S/R levels from H1 pivot clustering
        levels = {}
        try:
            sr = compute_sr_levels(h1_df, pivot_range=2)
            levels["resistance"] = [r["level"] for r in sr["resistance"]]
            levels["support"] = [s["level"] for s in sr["support"]]
            levels["sr_meta"] = sr
        except Exception as e:
            warnings.append(f"S/R computation failed, using fallback: {e}")
            high_20 = h1_row.get("high_20")
            low_20 = h1_row.get("low_20")
            atr = h1_row.get("atr")
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

        # Scoring
        score_result = self._m5_scalp_scoring_engine.score(
            h1_row, m5_tail, instrument.trading_sessions,
        )
        scoring = {
            "total_score": score_result["total_score"],
            "max_score": score_result["max_score"],
            "direction": score_result["direction"],
            "conviction": score_result["conviction"],
            "factors": score_result["factors"],
        }

        # Overlay real S/R proximity score
        if levels.get("sr_meta") and scoring.get("factors") is not None:
            sr_score = self._compute_sr_score_from_levels(
                current, levels["sr_meta"], h1_row.get("atr"),
            )
            if sr_score is not None:
                scoring["factors"]["sr_proximity"] = sr_score

        # Classify signal type
        scoring["signal_type"] = "scalp"

        # Session info
        session = _session_info(instrument)

        # Summary
        direction = scoring.get("direction", "NO TRADE")
        conviction = scoring.get("conviction")
        h1_trend = h1_block.get("trend", "?") if h1_block else "?"
        total = scoring.get("total_score", 0)

        summary = (
            f"{conviction or 'LOW'} conviction {direction or 'NO TRADE'}. "
            f"H1 trend {h1_trend}. "
            f"Score {total}/{scoring.get('max_score', 14)}."
        )

        result = {
            "instrument": key,
            "display_name": instrument.display_name,
            "mode": "m5_scalp",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "price": price_info,
            "technicals": {},
            "levels": levels,
            "scoring": scoring,
            "session": session,
            "summary": summary,
        }

        if h1_block:
            result["technicals"]["h1"] = h1_block
        if m5_block:
            result["technicals"]["m5"] = m5_block

        if warnings:
            result["warnings"] = warnings

        return result

    async def _run_intraday_analysis(self, key: str, instrument: InstrumentSpec) -> dict:
        """Execute intraday analysis: fetch 1H + 15m data, score."""
        warnings = []
        loop = asyncio.get_event_loop()
        symbol = instrument.yahoo_symbol

        # Fetch 1H (1 month) and 15m (5 days) concurrently
        h1_future = loop.run_in_executor(_executor, _fetch_ohlcv, symbol, "1mo", "1h")
        m15_future = loop.run_in_executor(_executor, _fetch_ohlcv, symbol, "5d", "15m")

        results = await asyncio.gather(h1_future, m15_future, return_exceptions=True)

        h1_df = results[0] if not isinstance(results[0], Exception) else pd.DataFrame()
        m15_df = results[1] if not isinstance(results[1], Exception) else pd.DataFrame()

        if isinstance(results[0], Exception):
            warnings.append(f"H1 fetch failed: {results[0]}")
        if isinstance(results[1], Exception):
            warnings.append(f"M15 fetch failed: {results[1]}")

        if h1_df.empty:
            return {"error": f"H1 data unavailable for {key}", "warnings": warnings}

        # Compute indicators
        h1_row = None
        h1_block = None
        if len(h1_df) >= 20:
            try:
                compute_indicators(h1_df)
                h1_block = _build_timeframe_block(h1_df, include_sma=True)
                h1_row = h1_df.iloc[-1]
            except Exception as e:
                warnings.append(f"H1 indicators failed: {e}")

        m15_row = None
        m15_block = None
        if not m15_df.empty and len(m15_df) >= 14:
            try:
                compute_indicators(m15_df)
                m15_block = _build_timeframe_block(m15_df)
                m15_row = m15_df.iloc[-1]
            except Exception as e:
                warnings.append(f"M15 indicators failed: {e}")

        if h1_row is None:
            return {"error": f"H1 indicators unavailable for {key}", "warnings": warnings}

        # Current price info
        current = float(h1_row["close"])
        prev_close = float(h1_df["close"].iloc[-2]) if len(h1_df) >= 2 else current
        change_pct = round((current - prev_close) / prev_close * 100, 2) if prev_close else 0
        price_info = {
            "current": round(current, 4 if current < 10 else 2),
            "previous_close": round(prev_close, 4 if prev_close < 10 else 2),
            "change_pct": change_pct,
        }

        # S/R levels from 1H pivot clustering
        levels = {}
        try:
            sr = compute_sr_levels(h1_df, pivot_range=2)
            levels["resistance"] = [r["level"] for r in sr["resistance"]]
            levels["support"] = [s["level"] for s in sr["support"]]
            levels["sr_meta"] = sr
        except Exception as e:
            warnings.append(f"S/R computation failed, using fallback: {e}")
            high_20 = h1_row.get("high_20")
            low_20 = h1_row.get("low_20")
            atr = h1_row.get("atr")
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

        # Scoring
        score_result = self._intraday_scoring_engine.score(
            h1_row, m15_row, instrument.trading_sessions,
        )
        scoring = {
            "total_score": score_result["total_score"],
            "max_score": score_result["max_score"],
            "direction": score_result["direction"],
            "conviction": score_result["conviction"],
            "factors": score_result["factors"],
        }

        # Overlay real S/R proximity score
        if levels.get("sr_meta") and scoring.get("factors") is not None:
            sr_score = self._compute_sr_score_from_levels(
                current, levels["sr_meta"], h1_row.get("atr"),
            )
            if sr_score is not None:
                scoring["factors"]["sr_proximity"] = sr_score
                # Recompute total score
                total_score = sum(
                    scoring["factors"].get(f, 0.0) * INTRADAY_FACTOR_WEIGHTS[f]
                    for f in INTRADAY_FACTOR_WEIGHTS
                )
                total_score = round(total_score, 2)
                scoring["total_score"] = total_score
                if total_score >= INTRADAY_SIGNAL_THRESHOLD:
                    scoring["direction"] = "BUY"
                elif total_score <= -INTRADAY_SIGNAL_THRESHOLD:
                    scoring["direction"] = "SELL"
                else:
                    scoring["direction"] = None
                abs_score = abs(total_score)
                if abs_score >= INTRADAY_HIGH_CONVICTION_THRESHOLD:
                    scoring["conviction"] = "HIGH"
                elif abs_score >= INTRADAY_SIGNAL_THRESHOLD:
                    scoring["conviction"] = "MEDIUM"
                else:
                    scoring["conviction"] = None

        # Classify signal type
        scoring["signal_type"] = self._classify_signal_type(
            scoring.get("factors", {}), mode="intraday",
        )

        # Calendar/news overlay for intraday (protect against trading into events)
        calendar_data = None
        news_data = None
        try:
            calendar_coro = self._calendar_service.get_calendar_risk(key)
            news_coro = self._news_service.get_news_sentiment(key)
            calendar_data, news_data = await asyncio.gather(
                calendar_coro, news_coro, return_exceptions=True,
            )
            if isinstance(calendar_data, Exception):
                warnings.append(f"Calendar fetch failed: {calendar_data}")
                calendar_data = None
            if isinstance(news_data, Exception):
                warnings.append(f"News fetch failed: {news_data}")
                news_data = None
        except Exception as e:
            warnings.append(f"Calendar/news fetch failed: {e}")

        # Apply calendar risk as score penalty (critical for intraday — avoid trading into NFP/FOMC)
        if calendar_data and "score" in calendar_data and calendar_data["score"] <= -1:
            cal_penalty = float(calendar_data["score"])  # -1 to -2
            scoring["total_score"] = round(scoring["total_score"] + cal_penalty * 2, 2)
            scoring["calendar_risk"] = cal_penalty
            # Re-check direction after penalty
            total_score = scoring["total_score"]
            if total_score >= INTRADAY_SIGNAL_THRESHOLD:
                scoring["direction"] = "BUY"
            elif total_score <= -INTRADAY_SIGNAL_THRESHOLD:
                scoring["direction"] = "SELL"
            else:
                scoring["direction"] = None
            abs_score = abs(total_score)
            if abs_score >= INTRADAY_HIGH_CONVICTION_THRESHOLD:
                scoring["conviction"] = "HIGH"
            elif abs_score >= INTRADAY_SIGNAL_THRESHOLD:
                scoring["conviction"] = "MEDIUM"
            else:
                scoring["conviction"] = None

        # Session info
        session = _session_info(instrument)

        # Summary
        direction = scoring.get("direction", "NO TRADE")
        conviction = scoring.get("conviction")
        h1_trend = h1_block.get("trend", "?") if h1_block else "?"
        h1_rsi = h1_block.get("rsi", "?") if h1_block else "?"
        total = scoring.get("total_score", 0)

        cal_ctx = ""
        if calendar_data and calendar_data.get("score", 0) <= -2:
            events = calendar_data.get("events", [])
            if events:
                cal_ctx = f" CAUTION: {events[0]['title']} in {events[0]['hours_away']}h."

        summary = (
            f"{conviction or 'LOW'} conviction {direction or 'NO TRADE'}. "
            f"H1 trend {h1_trend}. RSI {h1_rsi}. "
            f"Score {total}/{scoring.get('max_score', 16)}."
            f"{cal_ctx}"
        )

        result = {
            "instrument": key,
            "display_name": instrument.display_name,
            "mode": "intraday",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "price": price_info,
            "technicals": {},
            "levels": levels,
            "scoring": scoring,
            "session": session,
            "summary": summary,
        }

        if calendar_data:
            result["calendar"] = calendar_data
        if news_data:
            result["news"] = news_data

        if h1_block:
            result["technicals"]["h1"] = h1_block
        if m15_block:
            result["technicals"]["m15"] = m15_block

        if warnings:
            result["warnings"] = warnings

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

        # Support/Resistance levels from D1 pivot clustering
        levels = {}
        if d1_row is not None:
            try:
                sr = compute_sr_levels(d1_df, pivot_range=3)
                levels["resistance"] = [r["level"] for r in sr["resistance"]]
                levels["support"] = [s["level"] for s in sr["support"]]
                levels["sr_meta"] = sr
            except Exception as e:
                warnings.append(f"S/R computation failed, using fallback: {e}")
                high_20 = d1_row.get("high_20")
                low_20 = d1_row.get("low_20")
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

        # Scoring — dispatch based on instrument's swing_strategy
        scoring = {}
        if d1_row is not None:
            try:
                if instrument.swing_strategy == "rsi_reversal":
                    scoring = self._score_rsi_reversal(d1_row)
                else:
                    # Default: krabbe_scored (12-factor engine)
                    macro_series = self._macro_service.get_macro_series(key, "1y")
                    macro_row = None
                    if not macro_series.empty:
                        d1_date = d1_row.name
                        if hasattr(d1_date, "date"):
                            d1_date = d1_date.tz_localize(None) if d1_date.tzinfo else d1_date
                        macro_series.index = macro_series.index.tz_localize(None) if macro_series.index.tzinfo else macro_series.index
                        idx = macro_series.index.get_indexer([d1_date], method="ffill")
                        if idx[0] >= 0:
                            macro_row = macro_series.iloc[idx[0]]

                    score_result = self._scoring_engine.score_bar(d1_row, macro_row, key)
                    from app.services.scoring_engine import MAX_SCORE
                    scoring = {
                        "total_score": score_result["total_score"],
                        "max_score": MAX_SCORE,
                        "direction": score_result["direction"],
                        "conviction": score_result["conviction"],
                        "factors": score_result["factors"],
                    }
            except Exception as e:
                warnings.append(f"Scoring failed: {e}")

        # Live overlay: calendar, news, chart patterns
        calendar_data = None
        news_data = None
        pattern_data = None

        try:
            calendar_coro = self._calendar_service.get_calendar_risk(key)
            news_coro = self._news_service.get_news_sentiment(key)
            calendar_data, news_data = await asyncio.gather(
                calendar_coro, news_coro, return_exceptions=True,
            )
            if isinstance(calendar_data, Exception):
                warnings.append(f"Calendar fetch failed: {calendar_data}")
                calendar_data = None
            if isinstance(news_data, Exception):
                warnings.append(f"News fetch failed: {news_data}")
                news_data = None
        except Exception as e:
            warnings.append(f"Calendar/news fetch failed: {e}")

        if not d1_df.empty and len(d1_df) >= 20:
            try:
                pattern_data = detect_patterns(d1_df)
            except Exception as e:
                warnings.append(f"Pattern detection failed: {e}")

        # Overlay live values into scoring factors (krabbe_scored only)
        if scoring and "factors" in scoring and instrument.swing_strategy == "krabbe_scored":
            factors = scoring["factors"]

            if calendar_data and "score" in calendar_data:
                factors["calendar_risk"] = float(calendar_data["score"])

            if news_data and "score" in news_data:
                factors["news_sentiment"] = float(news_data["score"])

            if pattern_data and "enhanced_chart_score" in pattern_data:
                original = factors.get("chart_pattern", 0.0)
                enhanced = pattern_data["enhanced_chart_score"]
                factors["chart_pattern"] = round((original + enhanced) / 2, 2)

            # Recompute total score from updated factors
            total_score = sum(
                factors.get(f, 0.0) * FACTOR_WEIGHTS[f] for f in FACTOR_WEIGHTS
            )
            total_score = round(total_score, 2)
            scoring["total_score"] = total_score

            # Recompute direction and conviction
            if total_score >= SIGNAL_THRESHOLD:
                scoring["direction"] = "BUY"
            elif total_score <= -SIGNAL_THRESHOLD:
                scoring["direction"] = "SELL"
            else:
                scoring["direction"] = None

            abs_score = abs(total_score)
            if abs_score >= HIGH_CONVICTION_THRESHOLD:
                scoring["conviction"] = "HIGH"
            elif abs_score >= SIGNAL_THRESHOLD:
                scoring["conviction"] = "MEDIUM"
            else:
                scoring["conviction"] = None

            # Overlay real S/R proximity score
            if levels.get("sr_meta") and d1_row is not None:
                sr_score = self._compute_sr_score_from_levels(
                    float(d1_row["close"]), levels["sr_meta"], d1_row.get("atr"),
                )
                if sr_score is not None:
                    factors["sr_proximity"] = sr_score
                    total_score = sum(
                        factors.get(f, 0.0) * FACTOR_WEIGHTS[f]
                        for f in FACTOR_WEIGHTS
                    )
                    total_score = round(total_score, 2)
                    scoring["total_score"] = total_score
                    if total_score >= SIGNAL_THRESHOLD:
                        scoring["direction"] = "BUY"
                    elif total_score <= -SIGNAL_THRESHOLD:
                        scoring["direction"] = "SELL"
                    else:
                        scoring["direction"] = None
                    abs_score = abs(total_score)
                    if abs_score >= HIGH_CONVICTION_THRESHOLD:
                        scoring["conviction"] = "HIGH"
                    elif abs_score >= SIGNAL_THRESHOLD:
                        scoring["conviction"] = "MEDIUM"
                    else:
                        scoring["conviction"] = None

        # Classify signal type (RSI reversal sets its own signal_type)
        if scoring and "signal_type" not in scoring:
            scoring["signal_type"] = self._classify_signal_type(
                scoring.get("factors", {}), mode="swing",
            )

        # Session info
        session = _session_info(instrument)

        # Build summary
        direction = scoring.get("direction", "NO TRADE")
        conviction = scoring.get("conviction")
        total = scoring.get("total_score", 0)
        d1_trend = d1_block.get("trend", "?") if d1_block else "?"
        rsi_val = d1_block.get("rsi", "?") if d1_block else "?"

        # Calendar/news context for summary
        cal_ctx = ""
        if calendar_data and calendar_data.get("score", 0) <= -2:
            events = calendar_data.get("events", [])
            if events:
                cal_ctx = f" CAUTION: {events[0]['title']} in {events[0]['hours_away']}h."
        news_ctx = ""
        if news_data and abs(news_data.get("score", 0)) >= 1:
            sentiment = "bullish" if news_data["score"] > 0 else "bearish"
            news_ctx = f" News: {sentiment} ({news_data.get('net_sentiment', 0):+d} net)."

        summary = (
            f"{conviction or 'LOW'} conviction {direction or 'NO TRADE'}. "
            f"D1 trend {d1_trend}. RSI {rsi_val}. "
            f"Score {total}/{scoring.get('max_score', 28)}."
            f"{cal_ctx}{news_ctx}"
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

        if calendar_data:
            result["calendar"] = calendar_data
        if news_data:
            result["news"] = news_data
        if pattern_data:
            result["patterns"] = pattern_data

        if d1_block:
            result["technicals"]["d1"] = d1_block
        if h4_block:
            result["technicals"]["h4"] = h4_block
        if h1_block:
            result["technicals"]["h1"] = h1_block

        if warnings:
            result["warnings"] = warnings

        return result

    @staticmethod
    def _compute_sr_score_from_levels(
        current_price: float,
        sr_meta: dict,
        atr: float | None,
    ) -> float | None:
        """Compute S/R proximity score from real S/R levels. Range: -2 to +2.

        Positive = near support (good for buying), negative = near resistance.
        """
        if atr is None or pd.isna(atr) or atr == 0:
            return None

        atr = float(atr)
        supports = sr_meta.get("support", [])
        resistances = sr_meta.get("resistance", [])

        nearest_support_dist = None
        if supports:
            nearest_support_dist = (current_price - supports[0]["level"]) / atr

        nearest_resist_dist = None
        if resistances:
            nearest_resist_dist = (resistances[0]["level"] - current_price) / atr

        # Near support (good for buying)
        if nearest_support_dist is not None and nearest_support_dist < 0.5:
            return 1.5
        if nearest_support_dist is not None and nearest_support_dist < 1.0:
            return 0.5

        # Near resistance (good for selling)
        if nearest_resist_dist is not None and nearest_resist_dist < 0.5:
            return -1.5
        if nearest_resist_dist is not None and nearest_resist_dist < 1.0:
            return -0.5

        return 0.0

    @staticmethod
    def _score_rsi_reversal(d1_row: pd.Series) -> dict:
        """RSI reversal scoring for instruments where krabbe macro factors don't apply.

        Strategy: RSI < 30 + price > SMA200 → BUY, RSI > 70 + price < SMA200 → SELL.
        Returns same dict shape as krabbe scoring for compatibility.
        """
        RSI_SIGNAL = 7  # maps to score ±7 (matching SIGNAL_THRESHOLD)
        RSI_HIGH = 12   # maps to score ±12 (matching HIGH_CONVICTION_THRESHOLD)
        MAX_SCORE = 14.0  # max theoretical score for this simple strategy

        rsi = d1_row.get("rsi")
        sma200 = d1_row.get("sma200")
        sma20 = d1_row.get("sma20")
        sma50 = d1_row.get("sma50")
        close = d1_row.get("close")
        atr = d1_row.get("atr")

        factors = {
            "rsi_signal": 0.0,
            "trend_confirm": 0.0,
            "momentum": 0.0,
        }

        direction = None
        conviction = None
        total_score = 0.0

        if pd.isna(rsi) or rsi is None:
            return {
                "total_score": 0.0, "max_score": MAX_SCORE,
                "direction": None, "conviction": None,
                "factors": factors, "signal_type": "mean_reversion",
            }

        # RSI signal (core of the strategy)
        if rsi < 25:
            factors["rsi_signal"] = 2.0    # extreme oversold
        elif rsi < 30:
            factors["rsi_signal"] = 1.5    # oversold
        elif rsi < 35:
            factors["rsi_signal"] = 0.5    # mildly oversold
        elif rsi > 75:
            factors["rsi_signal"] = -2.0   # extreme overbought
        elif rsi > 70:
            factors["rsi_signal"] = -1.5   # overbought
        elif rsi > 65:
            factors["rsi_signal"] = -0.5   # mildly overbought

        # Trend confirmation (SMA200 alignment)
        if not pd.isna(sma200) and sma200 is not None and close is not None:
            if close > sma200:
                factors["trend_confirm"] = 1.0   # uptrend context
            elif close < sma200:
                factors["trend_confirm"] = -1.0  # downtrend context

        # Momentum (SMA20 vs SMA50)
        if not pd.isna(sma20) and not pd.isna(sma50):
            if sma20 > sma50:
                factors["momentum"] = 1.0
            elif sma20 < sma50:
                factors["momentum"] = -1.0

        # Composite score: RSI is the primary driver, trend/momentum confirm
        # BUY: oversold RSI + uptrend confirmation
        # SELL: overbought RSI + downtrend confirmation
        rsi_component = factors["rsi_signal"] * 3.0    # weight=3
        trend_component = factors["trend_confirm"] * 2.0  # weight=2
        mom_component = factors["momentum"] * 2.0      # weight=2
        total_score = round(rsi_component + trend_component + mom_component, 2)

        # Direction from total score (same thresholds as krabbe)
        if total_score >= RSI_SIGNAL:
            direction = "BUY"
        elif total_score <= -RSI_SIGNAL:
            direction = "SELL"

        abs_score = abs(total_score)
        if abs_score >= RSI_HIGH:
            conviction = "HIGH"
        elif abs_score >= RSI_SIGNAL:
            conviction = "MEDIUM"

        return {
            "total_score": total_score,
            "max_score": MAX_SCORE,
            "direction": direction,
            "conviction": conviction,
            "factors": factors,
            "signal_type": "mean_reversion",
        }

    @staticmethod
    def _classify_signal_type(factors: dict, mode: str = "intraday") -> str:
        """Classify signal as trend, mean_reversion, or mixed.

        Compares weighted magnitude of trend-following vs mean-reversion factors.
        """
        if mode == "intraday":
            trend_strength = (
                abs(factors.get("h1_trend", 0)) * 2
                + abs(factors.get("h1_momentum", 0)) * 1.5
            )
            mr_strength = (
                abs(factors.get("m15_entry", 0)) * 1.5
                + abs(factors.get("sr_proximity", 0)) * 1.0
            )
        else:  # swing
            trend_strength = (
                abs(factors.get("d1_trend", 0)) * 2
                + abs(factors.get("tf_alignment", 0)) * 0.5
            )
            mr_strength = (
                abs(factors.get("1h_entry", 0)) * 1.0
                + abs(factors.get("sr_proximity", 0)) * 1.0
            )

        total = trend_strength + mr_strength
        if total == 0:
            return "mixed"

        ratio = trend_strength / total
        if ratio > 0.65:
            return "trend"
        elif ratio < 0.35:
            return "mean_reversion"
        return "mixed"
