"""charts: KIS 일봉 + 결정·주문 마커 (계좌 필터는 주문 마커에만)."""

from __future__ import annotations

import time
from contextlib import closing
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request

from trader.broker.errors import KisApiError
from trader.orchestrator import tools
from trader.web.routers._common import KST, deps_of, require_market

router = APIRouter(prefix="/charts", tags=["charts"])

CACHE_TTL_S = 300.0
_candle_cache: dict[
    tuple, tuple[float, list[dict]]
] = {}  # (market, ticker, exchange, days, kst일) → (저장 시각, 봉)


@router.get("")
def charts(
    request: Request,
    ticker: str | None = None,
    market: str | None = None,
    exchange: str | None = None,
    days: int = Query(120, ge=1, le=1000),
    account_id: str | None = None,
) -> dict:
    m = require_market(market)
    try:
        tools._validate_ids(ticker or "", m)
    except tools.ToolInputError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    deps = deps_of(request)
    if account_id is not None and deps.account(account_id) is None:
        raise HTTPException(status_code=400, detail=f"알 수 없는 계좌: {account_id}")
    if deps.market is None:
        raise HTTPException(status_code=503, detail="시세 클라이언트 없음 (실전 앱키 필요)")
    if m == "US" and not exchange:
        raise HTTPException(status_code=400, detail="US 종목은 exchange(NAS|NYS|AMS)가 필요합니다")
    key = (m, ticker, exchange, days, deps.now().astimezone(KST).date().isoformat())
    hit = _candle_cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
        out_candles = hit[1]
    else:
        try:
            candles = (
                deps.market.get_daily_kr(ticker, days=days)
                if m == "KR"
                else deps.market.get_daily_us(ticker, exchange, days=days)
            )
        except KisApiError as e:
            raise HTTPException(status_code=502, detail=f"시세 조회 실패: {e}") from None
        out_candles = [
            {
                "time": c.date.isoformat(),
                "open": str(c.open),
                "high": str(c.high),
                "low": str(c.low),
                "close": str(c.close),
                "volume": c.volume,
            }
            for c in candles
        ]
        _candle_cache[key] = (time.monotonic(), out_candles)
    markers: list[dict] = []
    with closing(deps.conn_factory()) as conn:
        for r in conn.execute(
            "SELECT date, action, target_weight, confidence FROM decisions "
            "WHERE market=? AND ticker=? ORDER BY date",
            (m, ticker),
        ).fetchall():
            markers.append(
                {
                    "time": r["date"],
                    "kind": "decision",
                    "side": r["action"],
                    "text": f"결정 {r['action']} w={r['target_weight']} c={r['confidence']}",
                }
            )
        sql = (
            "SELECT account_id, side, qty, price, status, created_at FROM orders "
            "WHERE market=? AND ticker=? AND status NOT IN ('rejected','cancelled','failed')"
        )
        args: list = [m, ticker]
        if account_id:
            sql += " AND account_id=?"
            args.append(account_id)
        for r in conn.execute(sql + " ORDER BY id", args).fetchall():
            day = datetime.fromisoformat(r["created_at"]).astimezone(KST).date().isoformat()
            markers.append(
                {
                    "time": day,
                    "kind": "order",
                    "side": r["side"],
                    "text": f"{r['account_id']} {r['side']} {r['qty']}주 @{r['price'] or '시장가'}",
                }
            )
    return {"ticker": ticker, "market": m, "candles": out_candles, "markers": markers}
