"""charts: KIS 일봉·당일 분봉 + 결정·주문·트리거 마커 (계좌 필터는 주문 마커에만).

분봉 시간은 UTC epoch 초(lightweight-charts 의 분봉 형식)이며 KR 은 Asia/Seoul,
US 는 America/New_York 현지 시각(`xhms`)을 변환한다.
"""

from __future__ import annotations

import time
from contextlib import closing
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request

from trader.broker.errors import KisApiError
from trader.orchestrator import tools
from trader.web.routers._common import KST, deps_of, require_market

router = APIRouter(prefix="/charts", tags=["charts"])

CACHE_TTL_S = 300.0
MINUTE_CACHE_TTL_S = 60.0
MARKET_TZ = {"KR": KST, "US": ZoneInfo("America/New_York")}
_candle_cache: dict[
    tuple, tuple[float, list[dict]]
] = {}  # (market, ticker, exchange, days, kst일) → (저장 시각, 봉)
_minute_cache: dict[tuple, tuple[float, list[dict]]] = {}  # (market, ticker, exchange, count)


def _validate(
    request: Request,
    ticker: str | None,
    market: str | None,
    exchange: str | None,
    account_id: str | None,
):
    """두 차트 라우트가 공유하는 검증: market → 티커 형식 → 계좌 → 시세 클라이언트 → US exchange."""
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
    return m, deps


def _epoch(d: date, hhmmss: str, tz: ZoneInfo) -> int:
    t = datetime.strptime(hhmmss.ljust(6, "0"), "%H%M%S").time()
    return int(datetime.combine(d, t, tzinfo=tz).timestamp())


@router.get("")
def charts(
    request: Request,
    ticker: str | None = None,
    market: str | None = None,
    exchange: str | None = None,
    days: int = Query(120, ge=1, le=1000),
    account_id: str | None = None,
) -> dict:
    m, deps = _validate(request, ticker, market, exchange, account_id)
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


@router.get("/minute")
def minute_chart(
    request: Request,
    ticker: str | None = None,
    market: str | None = None,
    exchange: str | None = None,
    count: int = Query(120, ge=1, le=390),
    account_id: str | None = None,
) -> dict:
    m, deps = _validate(request, ticker, market, exchange, account_id)
    tz = MARKET_TZ[m]
    key = (m, ticker, exchange, count)
    hit = _minute_cache.get(key)
    if hit and time.monotonic() - hit[0] < MINUTE_CACHE_TTL_S:
        out_candles = hit[1]
    else:
        try:
            candles = (
                deps.market.get_minute_kr(ticker, count=count)
                if m == "KR"
                else deps.market.get_minute_us(ticker, exchange, count=min(count, 120))
            )
        except KisApiError as e:
            raise HTTPException(status_code=502, detail=f"시세 조회 실패: {e}") from None
        out_candles = [
            {
                "time": _epoch(c.date, c.time or "000000", tz),
                "date": c.date.isoformat(),
                "open": str(c.open),
                "high": str(c.high),
                "low": str(c.low),
                "close": str(c.close),
                "volume": c.volume,
            }
            for c in candles
        ]
        _minute_cache[key] = (time.monotonic(), out_candles)
    session_date = out_candles[-1]["date"] if out_candles else None
    markers: list[dict] = []
    if session_date:
        markers = _intraday_markers(deps, m, ticker, account_id, session_date, tz)
    return {
        "ticker": ticker,
        "market": m,
        "interval": "1m",
        "tz": str(tz.key),
        "session_date": session_date,
        "candles": [{k: v for k, v in c.items() if k != "date"} for c in out_candles],
        "markers": markers,
    }


def _intraday_markers(deps, m, ticker, account_id, session_date, tz) -> list[dict]:
    """session_date(시장 현지) 의 주문·트리거를 분 단위 내림 epoch 로. 결정은 시각이 없어 제외."""

    def minute_of(created_at: str) -> int | None:
        dt = datetime.fromisoformat(created_at).astimezone(tz)
        if dt.date().isoformat() != session_date:
            return None
        return int(dt.timestamp()) // 60 * 60

    out: list[dict] = []
    with closing(deps.conn_factory()) as conn:
        sql = (
            "SELECT account_id, side, qty, price, created_at FROM orders "
            "WHERE market=? AND ticker=? AND status NOT IN ('rejected','cancelled','failed')"
        )
        args: list = [m, ticker]
        if account_id:
            sql += " AND account_id=?"
            args.append(account_id)
        for r in conn.execute(sql + " ORDER BY id", args).fetchall():
            t = minute_of(r["created_at"])
            if t is not None:
                px = r["price"] or "시장가"
                text = f"{r['account_id']} {r['side']} {r['qty']}주 @{px}"
                out.append({"time": t, "kind": "order", "side": r["side"], "text": text})
        for r in conn.execute(
            "SELECT kind, detail, created_at FROM triggers WHERE market=? AND ticker=? ORDER BY id",
            (m, ticker),
        ).fetchall():
            t = minute_of(r["created_at"])
            if t is not None:
                out.append(
                    {
                        "time": t,
                        "kind": "trigger",
                        "side": "TRIGGER",
                        "text": f"트리거 {r['kind']}: {r['detail']}",
                    }
                )
    return sorted(out, key=lambda x: x["time"])
