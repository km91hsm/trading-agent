"""라우터 공통: 계좌·시장 파라미터 검증 (400), 연결 헬퍼."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request

from trader.config import AccountConfig

KST = ZoneInfo("Asia/Seoul")


def deps_of(request: Request):
    return request.app.state.deps


def require_account(request: Request, account_id: str | None) -> AccountConfig:
    if not account_id:
        raise HTTPException(status_code=400, detail="account_id 가 필요합니다")
    acct = deps_of(request).account(account_id)
    if acct is None:
        raise HTTPException(status_code=400, detail=f"알 수 없는 계좌: {account_id}")
    return acct


def require_market(market: str | None, account: AccountConfig | None = None) -> str:
    if market not in ("KR", "US"):
        raise HTTPException(status_code=400, detail="market 은 KR 또는 US 여야 합니다")
    if account is not None and market not in account.markets:
        raise HTTPException(
            status_code=400, detail=f"계좌 {account.id} 는 {market} 시장을 쓰지 않습니다"
        )
    return market


def today_kst(request: Request):
    return deps_of(request).now().astimezone(KST).date()
