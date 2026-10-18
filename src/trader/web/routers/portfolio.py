"""portfolio: 계좌·시장별 최신 스냅샷 + 최근 주문·미승인. refresh(토큰)는 브로커 잔고 재조회."""

from __future__ import annotations

from contextlib import closing

from fastapi import APIRouter, Depends, HTTPException, Request

from trader.broker.errors import KisApiError
from trader.ledger import views
from trader.web.auth import require_token
from trader.web.routers._common import deps_of, require_account, require_market, today_kst

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get("")
def portfolio(request: Request, account_id: str | None = None, market: str | None = None) -> dict:
    acct = require_account(request, account_id)
    m = require_market(market, acct)
    with closing(deps_of(request).conn_factory()) as conn:
        return {
            "account_id": acct.id,
            "market": m,
            "snapshot": views.latest_positions(conn, acct.id, m),
            "orders": views.list_orders(conn, acct.id, limit=20),
            "approvals": views.list_approvals(conn, acct.id, status="pending"),
        }


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
            build_portfolio(
                conn, broker, acct, m, today_kst(request), memory_settings=deps.settings.memory
            )
        except (KisApiError, ValueError) as e:
            raise HTTPException(status_code=502, detail=f"잔고 조회 실패: {e}") from None
        snap = views.latest_positions(conn, acct.id, m)
    return snap or {}
