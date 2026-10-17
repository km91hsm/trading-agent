"""05_quant 체크포인트 (스펙 §9.1·§1.4): 승격된 퀀트 모델의 확률을 트레이더 입력으로 만든다."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from trader.agents import schemas
from trader.ml import features
from trader.ml.models import quant

SIGNAL_THRESHOLD = 0.5


def signal_for(p_up: float, p_down: float) -> str:
    if p_up >= SIGNAL_THRESHOLD:
        return "BUY"
    if p_down >= SIGNAL_THRESHOLD:
        return "SELL"
    return "NEUTRAL"


def latest_row(
    ticker: str, market: str, state: Path | None = None, *, asof: date | None = None
) -> dict | None:
    """피처 스토어의 해당 종목 최신 행 (asof 이하, date 는 ISO 문자열)."""
    df = features.latest_rows(state, market, tickers=[ticker], asof=asof)
    if df.empty:
        return None
    r = df.iloc[0].to_dict()
    r["date"] = pd.Timestamp(r["date"]).date().isoformat()
    return r


def build(model: Any, meta: dict, row: dict, *, ticker: str, asof: str) -> dict:
    """QuantReport dump. 학습 피처 중 행에 없는 열은 NaN 으로 두고 data_gaps 에 적는다."""
    cols: list[str] = list(meta.get("features") or getattr(model, "features", []))
    X = quant.align(pd.DataFrame([row]), cols)
    p = model.predict_proba(X)[0]
    missing = [c for c in cols if c not in row or pd.isna(row.get(c))]
    gaps = [f"피처 없음: {', '.join(missing)}"] if missing else []
    horizon = int(((meta.get("label") or {}).get("horizon_days")) or 5)
    rep = schemas.QuantReport(
        ticker=ticker,
        asof=asof,
        role="quant",
        model=f"{meta.get('name', 'quant')}@{meta.get('version', '?')}",
        p_down=round(float(p[0]), 4),
        p_flat=round(float(p[1]), 4),
        p_up=round(float(p[2]), 4),
        signal=signal_for(float(p[2]), float(p[0])),
        horizon_days=horizon,
        features_used=cols,
        data_gaps=gaps,
        feature_date=str(row.get("date")) if row.get("date") is not None else None,
    )
    return rep.model_dump()
