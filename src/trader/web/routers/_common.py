"""라우터 공통: 계좌·시장 파라미터 검증 (400), 연결 헬퍼."""

from __future__ import annotations

import contextlib

from fastapi import HTTPException, Request

from trader.config import KST, AccountConfig


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


WEB_KIS_WAIT_S = 2.0  # 웹 요청이 KIS 레이트리미터에서 기다릴 최대 시간 (넘으면 503)


@contextlib.contextmanager
def kis_wait(market_or_broker):
    """웹 라우터의 KIS 호출을 짧은 대기 상한으로 감싼다. 데몬이 백필·모니터로 리미터를 점유 중이면
    차트·새로고침이 수십 초 매달리는 대신 503 '잠시 뒤 다시' 로 바로 돌아온다."""
    from trader.broker.kis_client import RateLimitBusy

    client = getattr(market_or_broker, "client", None)
    limiter = getattr(client, "limiter", None)
    if limiter is None:
        yield
        return
    try:
        with limiter.max_wait(WEB_KIS_WAIT_S):
            yield
    except RateLimitBusy as e:
        raise HTTPException(status_code=503, detail=f"{e} — 잠시 뒤 다시 시도") from None
