"""기술 지표 (pandas). 워밍업 구간은 NaN, latest() 는 NaN 을 None 으로 바꾼다.

비율은 전부 소수(0.05 = 5%). 입력 캔들은 날짜 오름차순이어야 한다.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from trader.broker.models import Candle

COLUMNS = [
    "sma_5",
    "sma_20",
    "sma_60",
    "sma_120",
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_hist",
    "kdj_k",
    "kdj_d",
    "kdj_j",
    "bb_upper",
    "bb_mid",
    "bb_lower",
    "atr_14",
    "sma_5_rel",
    "sma_20_rel",
    "sma_60_rel",
    "bb_pos",
    "atr_pct",
    "macd_hist_rel",
    "roc_10",
    "vol_z_20",
    "high_52w_pos",
]


def to_frame(candles: list[Candle]) -> pd.DataFrame:
    """캔들 목록 → date 인덱스의 float DataFrame(open, high, low, close, volume)."""
    if not candles:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"], dtype="float64")
    df = pd.DataFrame(
        {
            "open": [float(c.open) for c in candles],
            "high": [float(c.high) for c in candles],
            "low": [float(c.low) for c in candles],
            "close": [float(c.close) for c in candles],
            "volume": [float(c.volume) for c in candles],
        },
        index=pd.Index([c.date for c in candles], name="date"),
    )
    return df.sort_index()


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    # 손실이 0 이면 RSI 100
    rsi = rsi.where(avg_loss != 0, 100.0)
    rsi[avg_gain.isna()] = np.nan
    return rsi


def indicator_frame(df: pd.DataFrame) -> pd.DataFrame:
    """지표 컬럼을 붙인 새 DataFrame. 원본은 바꾸지 않는다."""
    out = df.copy()
    if out.empty:
        for c in COLUMNS:
            out[c] = pd.Series(dtype="float64")
        return out
    close, high, low, vol = out["close"], out["high"], out["low"], out["volume"]
    for n in (5, 20, 60, 120):
        out[f"sma_{n}"] = close.rolling(n, min_periods=n).mean()
    out["rsi_14"] = _rsi(close)
    ema12 = close.ewm(span=12, min_periods=12, adjust=False).mean()
    ema26 = close.ewm(span=26, min_periods=26, adjust=False).mean()
    out["macd"] = ema12 - ema26
    out["macd_signal"] = out["macd"].ewm(span=9, min_periods=9, adjust=False).mean()
    out["macd_hist"] = out["macd"] - out["macd_signal"]
    low9 = low.rolling(9, min_periods=9).min()
    high9 = high.rolling(9, min_periods=9).max()
    rsv = (close - low9) / (high9 - low9).replace(0, np.nan) * 100
    out["kdj_k"] = rsv.ewm(alpha=1 / 3, min_periods=9, adjust=False).mean()
    out["kdj_d"] = out["kdj_k"].ewm(alpha=1 / 3, min_periods=9, adjust=False).mean()
    out["kdj_j"] = 3 * out["kdj_k"] - 2 * out["kdj_d"]
    mid = close.rolling(20, min_periods=20).mean()
    std = close.rolling(20, min_periods=20).std(ddof=0)
    out["bb_mid"] = mid
    out["bb_upper"] = mid + 2 * std
    out["bb_lower"] = mid - 2 * std
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(
        axis=1
    )
    out["atr_14"] = tr.rolling(14, min_periods=14).mean()
    # 절대 가격 수준 지표의 close 대비 비율 (종목 간 일반화, Phase 8)
    for n in (5, 20, 60):
        out[f"sma_{n}_rel"] = close / out[f"sma_{n}"] - 1
    out["bb_pos"] = (close - out["bb_lower"]) / (out["bb_upper"] - out["bb_lower"]).replace(
        0, np.nan
    )
    out["atr_pct"] = out["atr_14"] / close
    out["macd_hist_rel"] = out["macd_hist"] / close
    out["roc_10"] = close / close.shift(10) - 1
    vol_mean = vol.rolling(20, min_periods=20).mean()
    vol_std = vol.rolling(20, min_periods=20).std(ddof=0)
    out["vol_z_20"] = (vol - vol_mean) / vol_std.replace(0, np.nan)
    hi252 = high.rolling(252, min_periods=252).max()
    lo252 = low.rolling(252, min_periods=252).min()
    out["high_52w_pos"] = (close - lo252) / (hi252 - lo252).replace(0, np.nan)
    return out


def latest(candles: list[Candle]) -> dict[str, float | None]:
    """마지막 봉의 지표. NaN/inf 는 None."""
    if not candles:
        return dict.fromkeys(COLUMNS)
    row = indicator_frame(to_frame(candles)).iloc[-1]
    out: dict[str, float | None] = {}
    for c in COLUMNS:
        v = row.get(c)
        out[c] = None if v is None or not math.isfinite(float(v)) else float(v)
    return out
