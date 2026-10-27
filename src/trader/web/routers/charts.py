"""charts: KIS 일봉·당일 분봉 + 결정·주문·트리거 마커 (계좌 필터는 주문 마커에만).

분봉 시간은 UTC epoch 초(lightweight-charts 의 분봉 형식)이며 KR 은 Asia/Seoul,
US 는 America/New_York 현지 시각(`xhms`)을 변환한다.
"""

from __future__ import annotations

import threading
import time
from contextlib import closing
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request

from trader.broker.errors import KisApiError
from trader.config import MARKET_TZ
from trader.ledger import repo
from trader.orchestrator import tools
from trader.web.routers._common import KST, deps_of, kis_wait, require_market

router = APIRouter(prefix="/charts", tags=["charts"])

CACHE_TTL_S = 300.0
MINUTE_CACHE_TTL_S = 60.0
_candle_cache: dict[
    tuple, tuple[float, list[dict]]
] = {}  # (market, ticker, exchange, days, kst일) → (저장 시각, 봉)
_minute_cache: dict[tuple, tuple[float, list[dict]]] = {}  # (market, ticker, exchange, count)
_cache_lock = threading.Lock()  # 요청 스레드가 여럿이라 dict 순회·삭제가 겹친다
_minute_locks: dict[tuple, threading.Lock] = {}  # 분봉 키별 조회 락(동시 미스를 KIS 1회로)


def _remember(
    cache: dict[tuple, tuple[float, list[dict]]],
    key: tuple,
    value: list[dict],
    *,
    ttl: float,
    now: float | None = None,
) -> None:
    """저장하면서 만료된 항목을 지운다 (일봉 캐시 키에 KST 날짜가 들어가 날마다 늘어난다)."""
    t = time.monotonic() if now is None else now
    with _cache_lock:
        for k in [k for k, (saved, _) in cache.items() if t - saved >= ttl]:
            del cache[k]
        cache[key] = (t, value)


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
    with _cache_lock:
        hit = _candle_cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
        out_candles = hit[1]
    else:
        try:
            with kis_wait(deps.market):
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
        _remember(_candle_cache, key, out_candles, ttl=CACHE_TTL_S)
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
            f"WHERE market=? AND ticker=? AND status NOT IN ({repo.dead_status_sql()})"
        )
        args: list = [m, ticker]
        if account_id:
            sql += " AND account_id=?"
            args.append(account_id)
        for r in conn.execute(sql + " ORDER BY id", args).fetchall():
            at = datetime.fromisoformat(r["created_at"]).astimezone(MARKET_TZ[m])
            day = at.date().isoformat()
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
    with _cache_lock:
        hit = _minute_cache.get(key)
    if hit and time.monotonic() - hit[0] < MINUTE_CACHE_TTL_S:
        out_candles = hit[1]
    else:
        out_candles = _minute_fetch(deps, m, ticker, exchange, count, tz, key)
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


def _minute_fetch(deps, m, ticker, exchange, count, tz, key) -> list[dict]:
    """분봉 캐시 미스: 키별 락 안에서 캐시를 다시 보고, 그래도 없을 때만 KIS 를 부른다 — 같은 차트를
    여러 탭·요청이 동시에 열어도 KIS 호출은 한 번(전체 검토 3차, quotes 와 같은 방식)."""
    with _minute_key_lock(key):
        with _cache_lock:
            hit = _minute_cache.get(key)
        if hit and time.monotonic() - hit[0] < MINUTE_CACHE_TTL_S:
            return hit[1]
        try:
            with kis_wait(deps.market):
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
        _remember(_minute_cache, key, out_candles, ttl=MINUTE_CACHE_TTL_S)
    return out_candles


def _minute_key_lock(key: tuple) -> threading.Lock:
    with _cache_lock:
        idle = [k for k, lk in _minute_locks.items() if k not in _minute_cache and not lk.locked()]
        for k in idle:
            if k != key:  # 쓰지 않는 키의 락은 지운다(캐시처럼 키가 날마다 늘지 않게)
                del _minute_locks[k]
        return _minute_locks.setdefault(key, threading.Lock())


def _intraday_markers(deps, m, ticker, account_id, session_date, tz) -> list[dict]:
    """session_date(시장 현지) 의 주문·트리거를 분 단위 내림 epoch 로. 결정은 시각이 없어 제외."""

    def minute_of(created_at: str) -> tuple[int, str] | None:
        """(분 단위 내림 epoch, 현지 HH:MM). 장 밖 시각 마커는 차트가 가장 가까운 봉에 붙이므로
        텍스트에 실제 시각을 남긴다."""
        dt = datetime.fromisoformat(created_at).astimezone(tz)
        if dt.date().isoformat() != session_date:
            return None
        return int(dt.timestamp()) // 60 * 60, dt.strftime("%H:%M")

    out: list[dict] = []
    # 그날(시장 현지) UTC 경계를 SQL 에 넣는다 — 종목의 모든 주문·트리거를 읽어 파이썬에서 거르지
    # 않게(전체 검토 3차). minute_of 가 현지 날짜를 한 번 더 확인한다
    lo, hi = repo.day_bounds(date.fromisoformat(session_date), m)
    with closing(deps.conn_factory()) as conn:
        sql = (
            "SELECT account_id, side, qty, price, created_at FROM orders "
            f"WHERE market=? AND ticker=? AND status NOT IN ({repo.dead_status_sql()}) "
            "AND created_at >= ? AND created_at < ?"
        )
        args: list = [m, ticker, lo, hi]
        if account_id:
            sql += " AND account_id=?"
            args.append(account_id)
        for r in conn.execute(sql + " ORDER BY id", args).fetchall():
            at = minute_of(r["created_at"])
            if at is not None:
                t, hhmm = at
                px = r["price"] or "시장가"
                text = f"{hhmm} {r['account_id']} {r['side']} {r['qty']}주 @{px}"
                out.append({"time": t, "kind": "order", "side": r["side"], "text": text})
        for r in conn.execute(
            "SELECT kind, detail, created_at FROM triggers WHERE market=? AND ticker=? "
            "AND created_at >= ? AND created_at < ? ORDER BY id",
            (m, ticker, lo, hi),
        ).fetchall():
            at = minute_of(r["created_at"])
            if at is not None:
                t, hhmm = at
                text = f"{hhmm} 트리거 {r['kind']}: {r['detail']}"
                out.append({"time": t, "kind": "trigger", "side": "TRIGGER", "text": text})
    return sorted(out, key=lambda x: x["time"])
