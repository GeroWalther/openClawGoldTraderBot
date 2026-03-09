"""Shared technical indicator computation used by backtester and technical analyzer."""

import pandas as pd


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute technical indicators on an OHLCV DataFrame.

    Adds columns: sma20, sma50, sma200, rsi, atr, high_20, low_20,
    macd, macd_signal, macd_hist, bb_mid, bb_upper, bb_lower, bb_bandwidth.

    Args:
        df: DataFrame with columns: open, high, low, close, volume.

    Returns:
        Same DataFrame with indicator columns added (mutated in place).
    """
    close = df["close"]
    high = df["high"]
    low = df["low"]

    # SMAs
    df["sma20"] = close.rolling(20).mean()
    df["sma50"] = close.rolling(50).mean()
    df["sma200"] = close.rolling(200).mean()

    # RSI
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, float("nan"))
    df["rsi"] = 100 - (100 / (1 + rs))

    # ATR
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()

    # 20-day high/low (for breakout)
    df["high_20"] = high.rolling(20).max()
    df["low_20"] = low.rolling(20).min()

    # MACD: EMA12 - EMA26, signal = EMA9 of MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    # Bollinger Bands: SMA20 +/- 2*stddev(20)
    df["bb_mid"] = df["sma20"]
    bb_std = close.rolling(20).std()
    df["bb_upper"] = df["bb_mid"] + 2 * bb_std
    df["bb_lower"] = df["bb_mid"] - 2 * bb_std
    df["bb_bandwidth"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]

    return df


def compute_scalp_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute fast scalp indicators on an M5 OHLCV DataFrame.

    Adds columns: ema9, ema21, rsi7.
    Should be called after compute_indicators() on M5 data.
    """
    close = df["close"]

    # Fast EMAs for crossover detection
    df["ema9"] = close.ewm(span=9, adjust=False).mean()
    df["ema21"] = close.ewm(span=21, adjust=False).mean()

    # Fast RSI(7) for momentum
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(7).mean()
    loss = (-delta.clip(upper=0)).rolling(7).mean()
    rs = gain / loss.replace(0, float("nan"))
    df["rsi7"] = 100 - (100 / (1 + rs))

    return df


def compute_sensei_indicators(
    df: pd.DataFrame,
    conv_thresh: float = 10.0,
    conv_bars: int = 8,
    db_tol: float = 6.0,
    pivot_lb: int = 3,
    db_min: int = 5,
    db_max: int = 80,
) -> pd.DataFrame:
    """Compute Sensei strategy indicators on an M15 OHLCV DataFrame.

    Adds columns: sma5, sma10, sma100, is_converged, conv_count,
    is_consolidating, pivot_low, pivot_high, w_active, m_active.

    Should be called after compute_indicators() which provides sma20, sma50, rsi, atr.

    Args:
        df: DataFrame with standard indicators already computed.
        conv_thresh: MA convergence threshold (% spread).
        conv_bars: Minimum bars of convergence for consolidation.
        db_tol: Double bottom/top tolerance (%).
        pivot_lb: Pivot lookback bars.
        db_min: Minimum bars between pivots.
        db_max: Maximum bars between pivots.
    """
    import numpy as np

    close = df["close"]
    low = df["low"].values
    high = df["high"].values

    # Additional SMAs (sma20, sma50 already from compute_indicators)
    df["sma5"] = close.rolling(5).mean()
    df["sma10"] = close.rolling(10).mean()
    df["sma100"] = close.rolling(100).mean()

    # MA convergence spread
    ma_df = df[["sma5", "sma10", "sma20", "sma50"]]
    spread = (ma_df.max(axis=1) - ma_df.min(axis=1)) / close * 100
    df["is_converged"] = spread <= conv_thresh

    # Consolidation count
    conv_count = np.zeros(len(df))
    for i in range(1, len(df)):
        conv_count[i] = conv_count[i - 1] + 1 if df["is_converged"].iloc[i] else 0
    df["conv_count"] = conv_count
    df["is_consolidating"] = conv_count >= conv_bars

    # Pivot lows & highs
    df["pivot_low"] = np.nan
    df["pivot_high"] = np.nan
    for i in range(pivot_lb, len(df) - pivot_lb):
        if all(low[i] <= low[i - j] and low[i] <= low[i + j] for j in range(1, pivot_lb + 1)):
            df.iloc[i, df.columns.get_loc("pivot_low")] = low[i]
        if all(high[i] >= high[i - j] and high[i] >= high[i + j] for j in range(1, pivot_lb + 1)):
            df.iloc[i, df.columns.get_loc("pivot_high")] = high[i]

    # Double bottom (W) — right bottom higher than left
    w_active = np.zeros(len(df), dtype=bool)
    bot1_p, bot1_i = np.nan, 0
    for i in range(len(df)):
        if not np.isnan(df["pivot_low"].iloc[i]):
            p = df["pivot_low"].iloc[i]
            if not np.isnan(bot1_p):
                d = i - bot1_i
                pct = abs(p - bot1_p) / bot1_p * 100
                consol = conv_count[i] >= conv_bars * 0.4
                if pct <= db_tol and db_min <= d <= db_max and p > bot1_p and consol:
                    for j in range(i, min(i + db_max, len(df))):
                        w_active[j] = True
            bot1_p, bot1_i = p, i
    df["w_active"] = w_active

    # Double top (M) — right top lower than left
    m_active = np.zeros(len(df), dtype=bool)
    top1_p, top1_i = np.nan, 0
    for i in range(len(df)):
        if not np.isnan(df["pivot_high"].iloc[i]):
            p = df["pivot_high"].iloc[i]
            if not np.isnan(top1_p):
                d = i - top1_i
                pct = abs(p - top1_p) / top1_p * 100
                consol = conv_count[i] >= conv_bars * 0.4
                if pct <= db_tol and db_min <= d <= db_max and p < top1_p and consol:
                    for j in range(i, min(i + db_max, len(df))):
                        m_active[j] = True
            top1_p, top1_i = p, i
    df["m_active"] = m_active

    return df
