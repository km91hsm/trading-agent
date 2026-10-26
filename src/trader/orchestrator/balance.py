"""잔고 주기 갱신(웹 실시간): 브로커 잔고를 다시 읽어 `positions_snapshot` 을 갱신한다 — 조회만,
주문 없음.

데몬 `balance_refresh` interval 잡이 정규장 동안 주기적으로, 웹 `POST /api/portfolio/ensure-fresh`
가 페이지 접속 때 스냅샷이 오래됐으면 부른다. 스냅샷 기록은 판정과 같은
`executor.build_portfolio`(시장별 운영일·NAV·메모리 브레이커 포함)를 그대로 쓴다. 새로 나타난
섹터 미분류 종목은 같은 자리에서 KIS 업종으로 자동 분류한다(관측할 때만, `sector_suggest`).
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from trader import market_hours
from trader.broker.errors import KisApiError
from trader.ledger import views
from trader.orchestrator import run_store

logger = logging.getLogger("trader.orchestrator")

STALE_AFTER = timedelta(minutes=2)  # 페이지 접속 때 이보다 오래된 스냅샷만 다시 읽는다
# 시장별 실행 중 표시: 데몬 잡과 웹 접속 갱신(같은 프로세스)이 같은 시장 잔고를 겹쳐 읽지 않게
_RUNNING: dict[str, threading.Lock] = {"KR": threading.Lock(), "US": threading.Lock()}


def snapshot_age(conn: sqlite3.Connection, account_id: str, market: str, now: datetime) -> float:
    """최신 스냅샷 기록 뒤 지난 초. 없으면 무한대."""
    row = conn.execute(
        "SELECT MAX(created_at) FROM positions_snapshot WHERE account_id=? AND market=?",
        (account_id, market),
    ).fetchone()
    if not row or not row[0]:
        return float("inf")
    try:
        return (now - datetime.fromisoformat(row[0])).total_seconds()
    except ValueError:
        return float("inf")


def refresh(
    conn: sqlite3.Connection,
    *,
    accounts: list,
    brokers: dict[str, Any],
    market: str,
    settings: Any,
    now: datetime,
    market_client: Any | None = None,
    state: Path | None = None,
    only_stale: bool = False,
    broker_ctx: Callable[[Any], Any] | None = None,
) -> dict:
    """시장 하나의 계좌별 잔고를 다시 읽는다. {refreshed: [id], skipped: [id], errors: {id: 사유},
    sectors: {종목: 섹터}}. 계좌 하나의 실패는 그 계좌만 errors 로. 같은 시장 갱신이 이미 돌고
    있으면 기다리지 않고 {busy: True}. broker_ctx 는 브로커 호출을 감싸는 컨텍스트(웹은
    `kis_wait` — 리미터가 점유 중이면 오래 매달리지 않는다)."""
    lock = _RUNNING.setdefault(market, threading.Lock())
    if not lock.acquire(blocking=False):
        return {"busy": True, "refreshed": [], "skipped": [], "errors": {}, "sectors": {}}
    try:
        return _refresh(
            conn,
            accounts=accounts,
            brokers=brokers,
            market=market,
            settings=settings,
            now=now,
            market_client=market_client,
            state=state,
            only_stale=only_stale,
            broker_ctx=broker_ctx or (lambda _b: contextlib.nullcontext()),
        )
    finally:
        lock.release()


def _refresh(
    conn,
    *,
    accounts,
    brokers,
    market,
    settings,
    now,
    market_client,
    state,
    only_stale,
    broker_ctx,
) -> dict:
    from trader.orchestrator.executor import build_portfolio

    out: dict = {"refreshed": [], "skipped": [], "errors": {}, "sectors": {}}
    tickers: dict[str, str | None] = {}
    for a in accounts:
        if market not in a.markets or a.id not in brokers:
            continue
        if only_stale and snapshot_age(conn, a.id, market, now) < STALE_AFTER.total_seconds():
            out["skipped"].append(a.id)
            continue
        try:
            with broker_ctx(brokers[a.id]):
                build_portfolio(
                    conn,
                    brokers[a.id],
                    a,
                    market,
                    run_store.run_date_for(market, now),
                    memory_settings=settings.memory,
                    killswitch_pct=settings.risk.killswitch_drawdown_pct,
                )
            out["refreshed"].append(a.id)
        except (KisApiError, ValueError) as e:
            out["errors"][a.id] = f"{type(e).__name__}: {e}"
            logger.warning("잔고 갱신 실패 %s %s: %s", a.id, market, e)
            continue
        snap = views.latest_positions(conn, a.id, market) or {}
        for p in snap.get("positions") or []:
            tickers.setdefault(str(p["ticker"]), p.get("exchange"))
    if tickers and market_client is not None:
        out["sectors"] = _classify(conn, market, tickers, settings, market_client, state)
    return out


def _classify(conn, market: str, tickers: dict, settings: Any, market_client: Any, state) -> dict:
    from trader import names as names_mod
    from trader.config import load_sectors
    from trader.scan import sector_suggest

    hedge = settings.excluded_tickers(market)
    try:
        smap = load_sectors()
    except Exception:  # noqa: BLE001 - 섹터 맵이 깨졌으면 분류를 건너뛴다
        return {}
    if market == "US":  # 스냅샷에는 거래소가 없다 → 유니버스의 거래소로 채운다(모르면 비워 둠)
        try:
            from trader.config import load_universe

            for u in load_universe().us:
                if u.symbol in tickers and not tickers[u.symbol]:
                    tickers[u.symbol] = u.exchange
        except Exception:  # noqa: BLE001 - 유니버스가 없으면 거래소 미상으로 둔다
            logger.debug("유니버스 거래소 조회 실패", exc_info=True)
    return sector_suggest.auto_classify(
        smap,
        market_client,
        market=market,
        tickers=[t for t in tickers if t not in hedge],
        state=state,
        exchanges={t: e for t, e in tickers.items() if e},
        names={
            t: n
            for (_m, t), n in names_mod.resolve_many(
                conn,
                (state / "cache") if state else names_mod.default_cache_dir(),
                [(market, t) for t in tickers],
            ).items()
        },
    )


def open_markets(markets: list[str], now: datetime) -> list[str]:
    return [m for m in markets if market_hours.is_open(m, now)]
