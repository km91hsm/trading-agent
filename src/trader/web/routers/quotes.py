"""quotes: 화면에 보이는 종목 현재가 (Phase 24 단계 2, 토큰 없음).

정규장(`market_hours.is_open`)에만 KIS 를 부르고, 같은 종목은 `QUOTE_TTL_S` 동안 캐시해 여러 탭·
요청이 KIS 호출을 늘리지 않게 한다. 장 밖·리미터 바쁨·조회 실패는 마지막 캐시 또는 최신 스냅샷
현재가를 `stale: true` 로 돌려준다(오류로 만들지 않는다 — 폴링이 오류 배너를 반복하지 않게).
국내는 실시간 허브(`web/live_quotes`, KIS 웹소켓)에 틱이 있으면 그 값을 먼저 쓰고(`source: "ws"`),
허브가 붙어 있으면 `stream: true` — 화면은 그때 1초마다 읽는다."""

from __future__ import annotations

import threading
import time
from contextlib import closing

from fastapi import APIRouter, HTTPException, Request

from trader import market_hours
from trader.orchestrator import tools
from trader.web.routers._common import deps_of, kis_wait

router = APIRouter(tags=["quotes"])

QUOTE_TTL_S = 5.0
MAX_TICKERS = 20
_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_lock = threading.Lock()
_key_locks: dict[tuple[str, str], threading.Lock] = {}  # 같은 종목 동시 조회를 한 번의 KIS 호출로


def _key_lock(key: tuple[str, str]) -> threading.Lock:
    with _lock:
        return _key_locks.setdefault(key, threading.Lock())


def _fresh(key: tuple[str, str]) -> tuple[float, dict] | None:
    with _lock:
        return _cache.get(key)


def _snapshot_prices(conn, market: str, tickers: list[str]) -> dict[str, dict]:
    """계좌 무관 최신 스냅샷 현재가(종목별 가장 최근 날짜)."""
    out: dict[str, dict] = {}
    for t in tickers:
        row = conn.execute(
            "SELECT current_price, date FROM positions_snapshot WHERE market=? AND ticker=? "
            "AND current_price IS NOT NULL ORDER BY date DESC, id DESC LIMIT 1",
            (market, t),
        ).fetchone()
        if row is not None:
            out[t] = {"price": row[0], "change_pct": None, "asof": row[1], "stale": True}
    return out


def _us_exchanges() -> dict[str, str]:
    """유니버스의 US 거래소 맵. 스냅샷 행에는 거래소가 없어 접미 없는 US 종목은 여기서 찾는다."""
    try:
        from trader.config import load_universe

        return {s.symbol: s.exchange for s in load_universe().us}
    except Exception:  # noqa: BLE001 - 유니버스가 없으면 NAS
        return {}


def _parse(market: str, tickers: str) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = []
    for raw in (x.strip() for x in tickers.split(",")):
        if not raw:
            continue
        t, _, ex = raw.partition(":")
        try:
            tools._validate_ids(t, market)
        except tools.ToolInputError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        if ex and ex not in ("NAS", "NYS", "AMS"):
            raise HTTPException(status_code=400, detail=f"거래소 {ex} 는 NAS|NYS|AMS 만")
        out.append((t, ex or None))
    if len(out) > MAX_TICKERS:
        raise HTTPException(status_code=400, detail=f"한 번에 {MAX_TICKERS}종목까지")
    return out


@router.get("/quotes")
def quotes(request: Request, market: str, tickers: str = "") -> dict:
    if market not in ("KR", "US"):
        raise HTTPException(status_code=400, detail="market 은 KR|US")
    deps = deps_of(request)
    items = _parse(market, tickers)
    now = deps.now()
    is_open = market_hours.is_open(market, now)
    out: dict[str, dict] = {}
    missing: list[str] = []
    hub = deps.quote_hub if market == "KR" else None
    if is_open and hub is not None:
        out.update(hub.request([t for t, _ in items]))
        items = [(t, ex) for t, ex in items if t not in out]
    if is_open and deps.market is not None:
        us_ex = _us_exchanges() if market == "US" else {}
        for t, ex in items:
            key = (market, t)
            hit = _fresh(key)
            if hit and time.monotonic() - hit[0] < QUOTE_TTL_S:
                out[t] = hit[1]
                continue
            # 종목별 락 안에서 캐시를 다시 본다: 먼저 온 요청이 받은 값을 뒤 요청이 쓴다
            with _key_lock(key):
                hit = _fresh(key)
                if hit and time.monotonic() - hit[0] < QUOTE_TTL_S:
                    out[t] = hit[1]
                    continue
                try:
                    with kis_wait(deps.market):
                        q = (
                            deps.market.get_price_kr(t)
                            if market == "KR"
                            else deps.market.get_price_us(t, ex or us_ex.get(t, "NAS"))
                        )
                except Exception:  # noqa: BLE001 - 바쁨(503)·KIS 오류는 마지막 값으로
                    if hit:
                        out[t] = {**hit[1], "stale": True}
                    else:
                        missing.append(t)
                    continue
                val = {
                    "price": str(q.price),
                    "change_pct": q.change_pct,
                    "asof": q.asof.isoformat(),
                    "stale": False,
                }
                with _lock:
                    _cache[key] = (time.monotonic(), val)
                out[t] = val
    else:
        missing = [t for t, _ in items]
    if missing:
        with closing(deps.conn_factory()) as conn:
            out.update(_snapshot_prices(conn, market, missing))
    return {
        "session": "open" if is_open else "closed",
        "asof": now.isoformat(),
        "quotes": out,
        "stream": bool(is_open and hub is not None and hub.status()["connected"]),
    }
