"""05_quant 체크포인트 (스펙 §9.1·§1.4): 승격된 퀀트 모델의 확률을 트레이더 입력으로 만든다."""

from __future__ import annotations

from datetime import date
from functools import lru_cache
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
    """피처 스토어의 해당 종목 최신 행 (asof 이하, date 는 ISO 문자열).

    한 tool 서브프로세스 안에서 같은 종목을 메모리·보정·05_quant 가 각각 조회하고, 스토어에 없는
    종목(관심종목·외부 보유분)은 조회마다 파일 60개를 읽으므로 (종목·시장·asof·최신 파일) 키로
    메모이즈한다. 새 파일이 생기거나 최신 파일이 다시 써지면(스냅샷이 종목 추가; 데몬처럼 오래 사는
    프로세스) 키가 바뀌어 무효화된다."""
    fdir = features._features_dir(state)
    files = sorted(fdir.glob("*.parquet")) if fdir.exists() else []
    newest = ""
    if files:
        st = files[-1].stat()
        newest = f"{files[-1].stem}:{st.st_mtime_ns}:{st.st_size}"
    row = _latest_row_cached(ticker, market, str(fdir), asof.isoformat() if asof else "", newest)
    return dict(row) if row is not None else None


@lru_cache(maxsize=256)
def _latest_row_cached(
    ticker: str, market: str, fdir: str, asof_iso: str, newest: str
) -> dict | None:
    asof = date.fromisoformat(asof_iso) if asof_iso else None
    df = features.latest_rows(Path(fdir).parent, market, tickers=[ticker], asof=asof)
    if df.empty:
        return None
    r = df.iloc[0].to_dict()
    r["date"] = pd.Timestamp(r["date"]).date().isoformat()
    return r


latest_row.cache_clear = _latest_row_cached.cache_clear  # type: ignore[attr-defined]


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
