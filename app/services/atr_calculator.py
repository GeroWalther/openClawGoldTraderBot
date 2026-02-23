import logging
import time

import yfinance as yf

from app.config import Settings
from app.instruments import InstrumentSpec

logger = logging.getLogger(__name__)


class ATRCalculator:
    """Computes ATR-based dynamic stop-loss and take-profit distances."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._cache: dict[str, tuple[float, float]] = {}  # key -> (atr, timestamp)

    def get_dynamic_sl_tp(
        self, instrument: InstrumentSpec
    ) -> tuple[float, float] | None:
        """
        Return (sl_distance, tp_distance) based on ATR, clamped to instrument bounds.

        Returns None on failure — caller should fall back to fixed defaults.
        """
        if not self.settings.atr_enabled:
            return None

        atr = self._get_atr(instrument)
        if atr is None:
            return None

        sl = atr * self.settings.atr_sl_multiplier
        tp = atr * self.settings.atr_tp_multiplier

        # Clamp to instrument bounds
        sl = max(instrument.min_stop_distance, min(sl, instrument.max_stop_distance))
        tp = max(sl, tp)  # TP must be at least as large as SL (1:1 R:R minimum)

        logger.info(
            "%s ATR(14)=%.4f → SL=%.4f, TP=%.4f",
            instrument.key, atr, sl, tp,
        )
        return sl, tp

    def _get_atr(self, instrument: InstrumentSpec) -> float | None:
        """Fetch ATR with caching (monotonic clock)."""
        now = time.monotonic()
        cached = self._cache.get(instrument.key)
        if cached:
            atr_val, ts = cached
            if now - ts < self.settings.atr_cache_ttl_seconds:
                return atr_val

        atr = self._fetch_atr(instrument)
        if atr is not None:
            self._cache[instrument.key] = (atr, now)
        return atr

    def _fetch_atr(self, instrument: InstrumentSpec) -> float | None:
        """Fetch daily OHLC from yfinance and compute ATR(period)."""
        try:
            ticker = yf.Ticker(instrument.yahoo_symbol)
            # Fetch enough data for ATR calculation
            df = ticker.history(period="1mo", interval="1d")
            if df is None or len(df) < self.settings.atr_period + 1:
                logger.warning(
                    "%s: insufficient data for ATR(%d) — got %d bars",
                    instrument.key, self.settings.atr_period,
                    len(df) if df is not None else 0,
                )
                return None

            # True Range = max(high-low, abs(high-prev_close), abs(low-prev_close))
            high = df["High"]
            low = df["Low"]
            close = df["Close"]
            prev_close = close.shift(1)

            tr1 = high - low
            tr2 = (high - prev_close).abs()
            tr3 = (low - prev_close).abs()

            import pandas as pd
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr = tr.rolling(window=self.settings.atr_period).mean().iloc[-1]

            if atr is None or atr != atr:  # NaN check
                return None

            return float(atr)
        except Exception as e:
            logger.warning("ATR fetch failed for %s: %s", instrument.key, e)
            return None
