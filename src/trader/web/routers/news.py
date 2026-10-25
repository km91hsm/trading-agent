"""news: 시장 전체 뉴스 저장소 조회 (읽기 전용, 토큰 없음). 데이터 페이지 '시장 뉴스' 표."""

from __future__ import annotations

from contextlib import closing
from datetime import UTC

from fastapi import APIRouter, HTTPException, Query, Request

from trader.sources import news_store
from trader.web.routers._common import deps_of

router = APIRouter(prefix="/news", tags=["news"])


@router.get("")
def list_news(
    request: Request,
    market: str = Query("KR"),
    hours: int = Query(48, ge=1, le=24 * 30),
    limit: int = Query(100, ge=1, le=500),
    ticker: str | None = None,
) -> dict:
    if market not in ("KR", "US"):
        raise HTTPException(status_code=400, detail=f"시장은 KR 또는 US: {market!r}")
    deps = deps_of(request)
    now = deps.now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    with closing(deps.conn_factory()) as conn:
        items = news_store.recent(conn, market, asof=now, hours=hours, limit=limit, ticker=ticker)
        total = news_store.count(conn, market)
    return {
        "market": market,
        "hours": hours,
        "total": total,
        "asof": now.isoformat(),
        "items": items,
    }
