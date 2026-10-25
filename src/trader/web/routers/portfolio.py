"""portfolio: 계좌·시장별 최신 스냅샷 + 최근 주문·미승인. refresh(토큰)는 브로커 잔고 재조회."""

from __future__ import annotations

from contextlib import closing

from fastapi import APIRouter, Depends, HTTPException, Request

from trader.broker.errors import KisApiError
from trader.ledger import views
from trader.orchestrator import run_store
from trader.web.auth import require_token
from trader.web.routers._common import deps_of, kis_wait, require_account, require_market

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get("")
def portfolio(request: Request, account_id: str | None = None, market: str | None = None) -> dict:
    acct = require_account(request, account_id)
    m = require_market(market, acct)
    deps = deps_of(request)
    with closing(deps.conn_factory()) as conn:
        snap = views.latest_positions(conn, acct.id, m)
        return {
            "account_id": acct.id,
            "market": m,
            "snapshot": snap,
            "sectors": _sectors(deps, m, snap),
            "max_sector_pct": deps.settings.risk.max_sector_pct,
            "orders": views.list_orders(conn, acct.id, limit=20),
            "approvals": views.list_approvals(conn, acct.id, status="pending"),
        }


def _sectors(deps, market: str, snap: dict | None) -> list[dict]:
    """섹터 비중 (Phase 17 S). sectors.yaml 을 못 읽으면 전부 '기타'."""
    if not snap:
        return []
    try:
        from trader.config import load_sectors

        smap = load_sectors()
        sector_of = lambda t: smap.sector_of(t, market)  # noqa: E731
    except Exception:  # noqa: BLE001 - 섹터 맵은 선택 자료
        sector_of = None
    hedge = deps.settings.hedge.tickers(market) if deps.settings.hedge.enabled else set()
    return views.sector_weights(
        snap["positions"], snap["nav"], sector_of, hedge_tickers=hedge, cash=snap.get("cash")
    )


@router.post("/refresh", dependencies=[Depends(require_token)])
def refresh(request: Request, account_id: str | None = None, market: str | None = None) -> dict:
    from trader.orchestrator.executor import build_portfolio

    acct = require_account(request, account_id)
    m = require_market(market, acct)
    deps = deps_of(request)
    broker = deps.brokers.get(acct.id)
    if broker is None:
        raise HTTPException(status_code=400, detail=f"계좌 {acct.id} 브로커 없음(앱키 미설정)")
    with closing(deps.conn_factory()) as conn:
        try:
            with kis_wait(broker):
                build_portfolio(
                    conn,
                    broker,
                    acct,
                    m,
                    run_store.run_date_for(m, deps.now()),  # executor·monitor 와 같은 운영일
                    memory_settings=deps.settings.memory,
                )
        except (KisApiError, ValueError) as e:
            raise HTTPException(status_code=502, detail=f"잔고 조회 실패: {e}") from None
        snap = views.latest_positions(conn, acct.id, m)
    return snap or {}
