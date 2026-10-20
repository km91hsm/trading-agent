"""features: 피처 스토어 행 탐색 (읽기 전용, 토큰 없음). 최신 행 표와 종목 이력."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request

from trader.config import ConfigError, load_sectors, load_universe
from trader.ml import features

router = APIRouter(prefix="/features", tags=["features"])
_TICKER_RE = re.compile(r"^[A-Za-z0-9.\-]{1,12}$")
COLUMNS = [*features.FEATURE_COLUMNS, "sector"]


def _market(value: str) -> str:
    if value not in ("KR", "US"):
        raise HTTPException(status_code=400, detail=f"시장은 KR 또는 US: {value!r}")
    return value


def _ticker(value: str) -> str:
    if not _TICKER_RE.match(value):
        raise HTTPException(status_code=400, detail=f"ticker 형식 오류: {value!r}")
    return value


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """NaN/NA → None, 날짜 → ISO (JSON 에 NaN 을 내보내지 않는다)."""
    out: list[dict[str, Any]] = []
    for row in df.to_dict(orient="records"):
        rec: dict[str, Any] = {}
        for k, v in row.items():
            if v is None or v is pd.NA or (isinstance(v, float) and pd.isna(v)):
                rec[k] = None
            elif isinstance(v, pd.Timestamp):
                rec[k] = v.date().isoformat()
            elif isinstance(v, date):
                rec[k] = v.isoformat()
            elif hasattr(v, "item"):  # numpy 스칼라
                rec[k] = None if pd.isna(v) else v.item()
            else:
                rec[k] = v
        out.append(rec)
    return out


def _universe_tickers(market: str) -> list[str] | None:
    try:
        u = load_universe()
    except ConfigError:
        return None
    members = u.kr if market == "KR" else [s.symbol for s in u.us]
    return members or None


@router.get("/latest")
def latest(request: Request, market: str) -> dict:
    """시장의 종목별 최신 피처 행 (+ config/sectors.yaml 섹터)."""
    deps = request.app.state.deps
    m = _market(market)
    df = features.latest_rows(deps.state, m, tickers=_universe_tickers(m))
    sectors = load_sectors()
    rows = _records(df)
    for r in rows:
        r["sector"] = sectors.sector_of(str(r["ticker"]), m)  # type: ignore[arg-type]
    asof = max((r["date"] for r in rows), default=None)
    return {"market": m, "asof": asof, "columns": COLUMNS, "rows": rows}


@router.get("/history")
def history(
    request: Request,
    market: str,
    ticker: str,
    days: int = Query(120, ge=5, le=1000),
) -> dict:
    """한 종목의 최근 days 행 (날짜 오름차순). 최신 파일부터 필요한 만큼만 읽는다."""
    deps = request.app.state.deps
    m = _market(market)
    t = _ticker(ticker)
    df = features.ticker_history(deps.state, m, t, days=days)
    return {"ticker": t, "market": m, "rows": _records(df)}
