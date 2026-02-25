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
