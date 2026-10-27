"""portfolio: 계좌·시장별 최신 스냅샷 + 최근 주문·미승인. refresh(토큰)는 브로커 잔고 재조회."""

from __future__ import annotations

import threading
import time
from contextlib import closing

from fastapi import APIRouter, Depends, HTTPException, Request

from trader.broker.errors import KisApiError
from trader.ledger import views
from trader.orchestrator import run_store
from trader.web.auth import require_token
from trader.web.routers._common import (
    csv_response,
    deps_of,
    kis_wait,
    require_account,
    require_market,
    require_same_origin,
)

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get("")
def portfolio(
    request: Request,
    account_id: str | None = None,
    market: str | None = None,
    format: str | None = None,
):
    acct = require_account(request, account_id)
    m = require_market(market, acct)
    deps = deps_of(request)
    with closing(deps.conn_factory()) as conn:
        snap = views.latest_positions(conn, acct.id, m)
        views.attach_plans(conn, m, snap)
        if format == "csv":  # Phase 23 G: 보유 표만
            return csv_response((snap or {}).get("positions") or [], f"positions_{acct.id}_{m}.csv")
        return {
            "account_id": acct.id,
            "market": m,
            "snapshot": snap,
            "sectors": _sectors(deps, m, snap),
            "max_sector_pct": deps.settings.risk.max_sector_pct,
            "rebalance_band_pct": deps.settings.rebalance.band_pct,
            "orders": views.list_orders(conn, acct.id, limit=20),
            "approvals": views.list_approvals(conn, acct.id, status="pending"),
        }


@router.get("/risk")
def risk(request: Request, account_id: str | None = None, market: str | None = None) -> dict:
    """위험 카드(Stock D-088): VaR·CVaR 95%·베타·상관·위험 기여. 피처 스토어 종가, 표시 전용."""
    from trader.ledger import risk_card
    from trader.orchestrator.run_store import run_date_for

    acct = require_account(request, account_id)
    m = require_market(market, acct)
    deps = deps_of(request)
    with closing(deps.conn_factory()) as conn:
        return {
            "account_id": acct.id,
            "market": m,
            **risk_card.cached_compute(
                conn,
                account_id=acct.id,
                market=m,
                today=run_date_for(m, deps.now()),
                state=deps.state,
            ),
        }


def _load_smap():
    try:
        from trader.config import load_sectors

        return load_sectors()
    except Exception:  # noqa: BLE001 - 섹터 맵은 선택 자료
        return None


_UNSET = object()


def _resolver(deps, market: str, smap=_UNSET):
    """yaml → 자동 분류 캐시(`sector_suggest.resolver`). 웹 조회 경로는 네트워크를 부르지 않는다.
    smap 을 주면 그 맵을 쓴다(한 요청에서 sectors.yaml 을 여러 번 읽지 않게)."""
    from trader.scan import sector_suggest

    return sector_suggest.resolver(
        _load_smap() if smap is _UNSET else smap, market, state=deps.state
    )


def _sectors(deps, market: str, snap: dict | None) -> list[dict]:
    """섹터 비중 (Phase 17 S). sectors.yaml 을 못 읽으면 전부 '기타'."""
    if not snap:
        return []
    sector_of = _resolver(deps, market)
    hedge = deps.settings.hedge.tickers(market) if deps.settings.hedge.enabled else set()
    return views.sector_weights(
        snap["positions"], snap["nav"], sector_of, hedge_tickers=hedge, cash=snap.get("cash")
    )


@router.get("/overview")
def overview(request: Request) -> dict:
    """전체 계좌 합산(Phase 24, 토큰 없음). 시장(통화)별로만 더한다."""
    deps = deps_of(request)
    smap = _load_smap()
    sector_of = smap.sector_of if smap is not None else None
    hedge = deps.settings.hedge

    def hedge_tickers(m: str) -> set[str]:
        return set(hedge.tickers(m)) if hedge.enabled else set()

    resolvers = {m: _resolver(deps, m, smap) for m in ("KR", "US")}
    with closing(deps.conn_factory()) as conn:
        return views.account_overview(
            conn,
            list(deps.accounts),
            sector_of=sector_of,
            hedge_tickers=hedge_tickers,
            sector_resolvers=resolvers,
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


ENSURE_MIN_INTERVAL_S = (
    30.0  # 페이지 접속 갱신 요청 최소 간격(시장별, 탭·새로고침이 몰려도 KIS 보호)
)
_ENSURE_LAST: dict[str, float] = {}
_ENSURE_LOCK = threading.Lock()


@router.post("/ensure-fresh")
def ensure_fresh(request: Request, market: str | None = None) -> dict:
    """페이지 접속 때(토큰 없음): 스냅샷이 오래된 계좌만(정규장 2분, 장 밖 10분 — 입출금·이체는
    장 밖에도 일어난다) 브로커 잔고를 다시 읽는다 — 조회만, 주문 없음. 시장별 30초에 한 번만
    실제로 돈다(그 사이 요청은 `throttled`).
    결과는 SSE `positions` 로 화면에 반영된다. 섹터 미분류 보유 종목은 같은 자리에서 자동 분류."""
    from trader.orchestrator import balance

    deps = deps_of(request)
    require_same_origin(request)
    m = (market or "").upper()
    if m not in ("KR", "US"):
        raise HTTPException(status_code=400, detail="market 은 KR|US")
    now = deps.now()
    stale_after = balance.STALE_AFTER if balance.open_markets([m], now) else balance.OFF_HOURS_STALE
    with _ENSURE_LOCK:
        last = _ENSURE_LAST.get(m, 0.0)
        if time.monotonic() - last < ENSURE_MIN_INTERVAL_S:
            return {"market": m, "throttled": True}
        _ENSURE_LAST[m] = time.monotonic()
    try:
        with closing(deps.conn_factory()) as conn:
            # 업종 자동 분류(KIS 추가 조회)는 데몬 잔고 갱신에만 — 토큰 없는 요청이 조회 안 늘림
            out = balance.refresh(
                conn,
                accounts=list(deps.accounts),
                brokers=deps.brokers,
                market=m,
                settings=deps.settings,
                now=now,
                market_client=None,
                state=deps.state,
                only_stale=True,
                stale_after=stale_after,
                broker_ctx=kis_wait,
            )
    except HTTPException as e:  # kis_wait 의 503(리미터 점유) — 다음 접속 때 다시
        return {"market": m, "busy": True, "detail": e.detail}
    finally:
        with _ENSURE_LOCK:  # 쿨다운은 완료 시각부터(긴 조회 뒤 바로 다시 들어오지 않게)
            _ENSURE_LAST[m] = time.monotonic()
    return {"market": m, **out}


lots_router = APIRouter(tags=["portfolio"])


@lots_router.get("/tax-lots")
def tax_lots(
    request: Request,
    account_id: str | None = None,
    market: str | None = None,
    year: int | None = None,
    format: str | None = None,
):
    """세금 로트·워시세일(Phase 25, 표시 전용). FIFO 실현 로트·보유 로트·요약, `?format=csv` 는
    실현 로트 표."""
    from trader.ledger import lots

    acct = require_account(request, account_id)
    m = require_market(market, acct)
    deps = deps_of(request)
    with closing(deps.conn_factory()) as conn:
        out = lots.report(conn, acct.id, m, year=year)
    if format == "csv":
        return csv_response(
            out["realized"],
            f"tax_lots_{acct.id}_{m}{'_' + str(year) if year else ''}.csv",
            columns=[
                "ticker",
                "qty",
                "buy_date",
                "sell_date",
                "cost",
                "proceeds",
                "pnl",
                "term",
                "wash_sale",
            ],
        )
    return out
