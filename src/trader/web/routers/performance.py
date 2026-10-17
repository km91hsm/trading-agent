"""performance: 계좌·시장별 성과 리포트 (스펙 §13)."""

from __future__ import annotations

from contextlib import closing

from fastapi import APIRouter, Query, Request

from trader.ledger import performance
from trader.web.routers._common import deps_of, require_account, require_market

router = APIRouter(prefix="/performance", tags=["performance"])


@router.get("")
def performance_report(
    request: Request,
    account_id: str | None = None,
    market: str | None = None,
    days: int = Query(30, ge=1, le=3650),
) -> dict:
    acct = require_account(request, account_id)
    m = require_market(market, acct)
    deps = deps_of(request)
    with closing(deps.conn_factory()) as conn:
        return performance.report(conn, deps.state, acct.id, m, days=days)
