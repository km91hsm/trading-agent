"""names: 종목명 조회 (원장 ticker_names → 캐시 JSON, 네트워크 없음, 토큰 없음)."""

from __future__ import annotations

from contextlib import closing

from fastapi import APIRouter, HTTPException, Query, Request

from trader import names
from trader.ledger import repo
from trader.orchestrator import tools
from trader.platform import paths
from trader.web.routers._common import deps_of

router = APIRouter(tags=["names"])


@router.get("/names")
def ticker_names(
    request: Request,
    market: str | None = Query(None),
    tickers: str | None = Query(None, description="쉼표 구분. 없으면 원장 전체"),
) -> dict[str, str]:
    deps = deps_of(request)
    cache_dir = (deps.state if deps.state is not None else paths.state_dir()) / "cache"
    with closing(deps.conn_factory()) as conn:
        if not tickers:
            found = repo.ticker_names(conn, market=market)
        else:
            if market not in ("KR", "US"):
                raise HTTPException(status_code=400, detail="tickers 를 주면 market 은 KR|US 필수")
            keys = []
            for t in (x.strip() for x in tickers.split(",")):
                if not t:
                    continue
                try:
                    tools._validate_ids(t, market)
                except tools.ToolInputError as e:
                    raise HTTPException(status_code=400, detail=str(e)) from None
                keys.append((market, t))
            found = names.resolve_many(conn, cache_dir, keys)
    return {f"{m}:{t}": n for (m, t), n in found.items()}
