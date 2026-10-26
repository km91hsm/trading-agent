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


@router.get("/names/search")
def search_names(
    request: Request,
    q: str = Query("", max_length=40),
    market: str | None = Query(None),
    limit: int = Query(20, ge=1, le=50),
) -> list[dict]:
    """종목 코드·이름 부분 일치 (Phase 24). 보유·관심·유니버스 우선, 원장·캐시(상장 전체)까지."""
    from trader.config import load_universe
    from trader.control import control

    if market not in (None, "KR", "US"):
        raise HTTPException(status_code=400, detail="market 은 KR|US")
    deps = deps_of(request)
    cache_dir = (deps.state if deps.state is not None else paths.state_dir()) / "cache"
    universe: dict[tuple[str, str], str | None] = {}
    try:
        u = load_universe()
        universe.update({("KR", t): None for t in u.kr})
        universe.update({("US", s.symbol): s.exchange for s in u.us})
    except Exception:  # noqa: BLE001 - 유니버스 파일이 없어도 검색은 된다
        pass
    with closing(deps.conn_factory()) as conn:
        held = {
            (r["market"], r["ticker"])
            for r in conn.execute(
                "SELECT DISTINCT market, ticker FROM positions_snapshot WHERE qty > 0 "
                "AND ticker NOT LIKE '\\_CASH%' ESCAPE '\\' AND market IS NOT NULL"
            )
        }
        watch = {(m, t) for m in ("KR", "US") for t in control.get_watchlist(conn, market=m)}
        return names.search(
            conn,
            cache_dir,
            q,
            market=market,
            universe=universe,
            held=held,
            watch=watch,
            limit=limit,
        )
