"""orders: 주문·승인·판정 조회 (계좌 필터 필수)."""

from __future__ import annotations

from contextlib import closing

from fastapi import APIRouter, Query, Request

from trader.ledger import views
from trader.web.routers._common import deps_of, require_account

router = APIRouter(tags=["orders"])


@router.get("/orders")
def orders(
    request: Request, account_id: str | None = None, limit: int = Query(100, ge=1, le=500)
) -> list[dict]:
    acct = require_account(request, account_id)
    with closing(deps_of(request).conn_factory()) as conn:
        return views.list_orders(conn, acct.id, limit=limit)


@router.get("/approvals")
def approvals(
    request: Request, account_id: str | None = None, status: str | None = None
) -> list[dict]:
    acct = require_account(request, account_id)
    with closing(deps_of(request).conn_factory()) as conn:
        return views.list_approvals(conn, acct.id, status=status)


@router.get("/risk-checks")
def risk_checks(
    request: Request, account_id: str | None = None, limit: int = Query(100, ge=1, le=500)
) -> list[dict]:
    acct = require_account(request, account_id)
    with closing(deps_of(request).conn_factory()) as conn:
        return views.list_risk_checks(conn, acct.id, limit=limit)
