"""학습 데이터셋 (스펙 §9.1): 피처 스토어 적재, 5거래일 수익률 3분류 라벨, 피처 열 선택.

라벨은 같은 종목의 `horizon_days` 뒤 종가로만 만든다(룩어헤드는 라벨에만, 피처에는 없음).
미래 봉이 없는 마지막 행들은 라벨이 NaN 이라 학습·평가에서 빠진다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

from trader.ml import features

LABELS = ("down", "flat", "up")
TARGET_COLUMNS = ("fwd_return", "fwd_date", "label")
NON_FEATURE_COLUMNS = ("date", "ticker", "market", "close", "volume", *TARGET_COLUMNS)


def load_all(
    state: Path | None = None,
    *,
    market: str | None = None,
    since: date | None = None,
    until: date | None = None,
) -> pd.DataFrame:
    """parquet 을 합쳐 (ticker, date) 로 정렬. date 는 datetime64.

    since/until 을 주면 파일명(`<date>.parquet`)으로 그 창의 파일만 읽는다 (성과·만기 계산처럼
    일부 구간만 필요한 호출이 전체 스토어를 읽지 않도록). 학습은 전체를 읽는다.
    """
    fdir = features._features_dir(state)
    files = sorted(fdir.glob("*.parquet")) if fdir.exists() else []
    if since is not None:
        files = [f for f in files if f.stem >= since.isoformat()]
    if until is not None:
        files = [f for f in files if f.stem <= until.isoformat()]
    if not files:
        return pd.DataFrame(columns=features.FEATURE_COLUMNS)
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    if market:
        df = df[df["market"] == market]
    if df.empty:
        return df.reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values(["ticker", "date"]).reset_index(drop=True)


LabelMode = Literal["absolute", "relative_tercile"]
MIN_CROSS_SECTION = 3  # 상대 라벨에 필요한 같은 날·같은 시장 종목 수


def make_labels(
    df: pd.DataFrame,
    *,
    horizon_days: int,
    up: float,
    down: float,
    mode: LabelMode = "absolute",
) -> pd.DataFrame:
    """fwd_return = close(t+h)/close(t) − 1 (종목별 shift). label 은 모드에 따라:
    - absolute: 0 down(≤down) / 1 flat / 2 up(≥up)
    - relative_tercile: 같은 (date, market) 안 fwd_return 순위 하위 1/3 → 0, 중간 → 1, 상위 1/3 → 2.
      같은 날 종목이 MIN_CROSS_SECTION 미만이면 NaN. up/down 은 쓰지 않는다.

    fwd_date 는 라벨이 쓴 미래 봉의 날짜다. 종목별 행 시프트라 날짜가 비는 종목(거래정지·결측)은
    h 거래일보다 멀리 볼 수 있어, 워크포워드 split 이 이 날짜로 테스트 창 침범을 걸러낸다."""
    out = df.sort_values(["ticker", "date"]).reset_index(drop=True).copy()
    close = pd.to_numeric(out["close"], errors="coerce")
    grouped = out.groupby("ticker")
    future = close.groupby(out["ticker"]).shift(-horizon_days)
    out["fwd_date"] = grouped["date"].shift(-horizon_days)
    out["fwd_return"] = (future / close - 1).astype("float64")
    if mode == "relative_tercile":
        label = _tercile_labels(out)
    else:
        label = pd.Series(
            np.where(out["fwd_return"] >= up, 2, np.where(out["fwd_return"] <= down, 0, 1)),
            index=out.index,
            dtype="float64",
        )
    label[out["fwd_return"].isna()] = np.nan
    out["label"] = label
    return out


def _tercile_labels(out: pd.DataFrame) -> pd.Series:
    """(date, market) 그룹 안 fwd_return 백분위 순위 → 0/1/2. 그룹이 작으면 NaN."""
    valid = out[out["fwd_return"].notna()]
    keys = [pd.to_datetime(valid["date"]), valid["market"]]
    pct = valid["fwd_return"].groupby(keys).rank(pct=True, method="average")  # 동률은 같은 라벨
    size = valid["fwd_return"].groupby(keys).transform("size")
    label = pd.Series(np.nan, index=out.index, dtype="float64")
    tercile = np.where(pct <= 1 / 3, 0.0, np.where(pct > 2 / 3, 2.0, 1.0))
    label.loc[valid.index] = np.where(size >= MIN_CROSS_SECTION, tercile, np.nan)
    return label


def feature_columns(df: pd.DataFrame) -> list[str]:
    """수치 피처 열만 (식별자·원가격·거래량·타깃 제외). 타깃이 섞이면 ValueError."""
    cols = []
    for c in df.columns:
        if c in NON_FEATURE_COLUMNS:
            continue
        if pd.api.types.is_numeric_dtype(df[c]) and df[c].notna().any():
            cols.append(c)  # 전부 NaN 인 열(아직 채우지 않은 피처)은 제외
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
