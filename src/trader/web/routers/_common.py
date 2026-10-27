"""라우터 공통: 계좌·시장 파라미터 검증 (400), 연결 헬퍼."""

from __future__ import annotations

import contextlib
import csv
import io

from fastapi import HTTPException, Request
from fastapi.responses import Response

from trader.config import KST, AccountConfig


def deps_of(request: Request):
    return request.app.state.deps


def require_same_origin(request: Request) -> None:
    """토큰 없는 쓰기성 호출(페이지 접속 갱신)을 다른 사이트(no-cors 단순 요청)가 부르지 못하게
    같은 출처만 — 헤더가 없는 비브라우저 호출(테스트·CLI)은 통과(전체 검토 2026-10-27)."""
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        raise HTTPException(status_code=403, detail="같은 출처 화면에서만 부를 수 있습니다")


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


def csv_response(rows: list[dict], filename: str, *, columns: list[str] | None = None) -> Response:
    """표 → CSV(UTF-8 BOM, 엑셀 한글 호환). 열 순서는 columns(없으면 첫 행 키 순)."""
    cols = columns or (list(rows[0].keys()) if rows else [])
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in cols})
    return Response(
        "\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
