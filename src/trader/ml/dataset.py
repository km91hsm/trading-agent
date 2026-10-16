"""학습 데이터셋 (스펙 §9.1): 피처 스토어 적재, 5거래일 수익률 3분류 라벨, 피처 열 선택.

라벨은 같은 종목의 `horizon_days` 뒤 종가로만 만든다(룩어헤드는 라벨에만, 피처에는 없음).
미래 봉이 없는 마지막 행들은 라벨이 NaN 이라 학습·평가에서 빠진다.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from trader.ml import features

LABELS = ("down", "flat", "up")
TARGET_COLUMNS = ("fwd_return", "label")
NON_FEATURE_COLUMNS = ("date", "ticker", "market", "close", "volume", *TARGET_COLUMNS)


def load_all(state: Path | None = None, *, market: str | None = None) -> pd.DataFrame:
    """모든 parquet 을 합쳐 (ticker, date) 로 정렬. date 는 datetime64."""
    fdir = features._features_dir(state)
    files = sorted(fdir.glob("*.parquet")) if fdir.exists() else []
    if not files:
        return pd.DataFrame(columns=features.FEATURE_COLUMNS)
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    if market:
        df = df[df["market"] == market]
    if df.empty:
        return df.reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values(["ticker", "date"]).reset_index(drop=True)


def make_labels(df: pd.DataFrame, *, horizon_days: int, up: float, down: float) -> pd.DataFrame:
    """fwd_return = close(t+h)/close(t) − 1 (종목별 shift), label = 0 down / 1 flat / 2 up."""
    out = df.sort_values(["ticker", "date"]).reset_index(drop=True).copy()
    close = pd.to_numeric(out["close"], errors="coerce")
    future = close.groupby(out["ticker"]).shift(-horizon_days)
    out["fwd_return"] = (future / close - 1).astype("float64")
    label = pd.Series(
        np.where(out["fwd_return"] >= up, 2, np.where(out["fwd_return"] <= down, 0, 1)),
        index=out.index,
        dtype="float64",
    )
    label[out["fwd_return"].isna()] = np.nan
    out["label"] = label
    return out


def feature_columns(df: pd.DataFrame) -> list[str]:
    """수치 피처 열만 (식별자·원가격·거래량·타깃 제외). 타깃이 섞이면 ValueError."""
    cols = []
    for c in df.columns:
        if c in NON_FEATURE_COLUMNS:
            continue
        if pd.api.types.is_numeric_dtype(df[c]):
            cols.append(c)
    return cols


def split_xy(df: pd.DataFrame, cols: list[str]) -> tuple[np.ndarray, np.ndarray]:
    """label 이 있는 행만 (X float64 with NaN, y int)."""
    for c in cols:
        if c in TARGET_COLUMNS:
            raise ValueError(f"타깃 열이 피처에 섞였습니다: {c}")
    labeled = df[df["label"].notna()]
    X = labeled[cols].apply(pd.to_numeric, errors="coerce").to_numpy(dtype="float64")
    y = labeled["label"].to_numpy(dtype="int64")
    return X, y
