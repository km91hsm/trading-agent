"""합성 피처 생성기. signal=True 면 roc_10 이 5일 뒤 수익과 양의 상관을 갖는다."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from trader.ml import features as feat


def trading_days(n: int, start: date = date(2025, 1, 2)) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def synthetic_features(
    n_tickers: int = 5, n_days: int = 300, *, signal: bool, seed: int = 0
) -> pd.DataFrame:
    """signal=True 면 AR(1) 잠재 추세가 수익률을 만들고 rsi_14·roc_10 에 드러난다(예측 가능).
    signal=False 면 피처와 수익이 독립이다(예측 불가, 역검증용)."""
    rng = np.random.default_rng(seed)
    days = trading_days(n_days)
    rows = []
    for t in range(n_tickers):
        regime = np.zeros(n_days)
        for i in range(1, n_days):
            regime[i] = 0.95 * regime[i - 1] + rng.normal(0, 0.3)
        if signal:
            drift = regime * 0.006 + rng.normal(0, 0.004, size=n_days)
            rsi = 50 + regime * 12 + rng.normal(0, 2, size=n_days)
        else:
            drift = rng.normal(0, 0.012, size=n_days)
            rsi = rng.uniform(20, 80, size=n_days)
        close = 100 * np.cumprod(1 + drift)
        for i, d in enumerate(days):
            roc = close[i] / close[i - 10] - 1 if i >= 10 else np.nan
            rows.append(
                {
                    "date": d,
                    "ticker": f"T{t:02d}",
                    "market": "KR",
                    "close": float(close[i]),
                    "volume": int(rng.integers(100_000, 1_000_000)),
                    "roc_10": float(roc * 100) if roc == roc else np.nan,
                    "rsi_14": float(np.clip(rsi[i], 0, 100)),
                    "atr_14": float(close[i] * 0.02),
                    "vol_z_20": float(rng.normal()),
                    "high_52w_pos": float(rng.uniform(0, 1)),
                }
            )
    df = pd.DataFrame(rows)
    for c in feat.FEATURE_COLUMNS:
        if c not in df.columns:
            df[c] = np.nan
    return df[feat.FEATURE_COLUMNS]
