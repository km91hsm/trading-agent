"""체결 동기화 (Phase 6): 브로커 체결 조회 → fills 멱등 적재 → orders.status 갱신.

주문번호가 원장에 없는 체결(앱·HTS 수동 주문)은 mode='external' 주문 행을 만들어 붙인다.
그날 체결 조회를 마친 뒤(마감 이후) 체결이 없는 accepted·unknown 주문은 expired 로 바꾼다 —
당일 주문은 마감에 소멸하므로 "조회 성공 + 체결 없음" 이 곧 "주문이 죽었다" 이고, 그래야 미체결
주문이 중복 방지 조건에 영구히 남지 않는다. 장중 수동 실행이나 조회 실패 때는 건드리지 않는다.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from trader.broker.models import Fill
from trader.config import MARKET_TZ, AccountConfig
from trader.ledger import repo

logger = logging.getLogger("trader.fills")

MARKET_CLOSE: dict[str, time] = {"KR": time(15, 30), "US": time(16, 0)}  # 현지 정규장 마감
US_EARLY_CLOSE = time(13, 0)  # NYSE 조기 마감일 (daemon.us_early_closes)


def market_close(market: str, day: date) -> time:
    """그날의 정규장 마감 시각(현지). US 조기 마감일은 13:00."""
    if market == "US":
        from trader.daemon import us_early_closes

        if day in us_early_closes(day.year):
            return US_EARLY_CLOSE
    return MARKET_CLOSE.get(market, time(23, 59))


def _order_id_for(
    conn: sqlite3.Connection, account_id: str, f: Fill, *, notifier: Any | None = None
) -> int:
    # KIS 주문번호(ODNO)는 날마다 다시 시작하므로 종목·방향이 같고 **같은 현지 날짜**에 낸 주문만
    # 같은 주문으로 본다 (옛 주문에 붙이면 그 주문의 기존 체결을 지워 버린다)
    local_day = _day_for(f.market, f.filled_at)
    lo, hi = repo.day_bounds(local_day, f.market)
    row = conn.execute(
        "SELECT id FROM orders WHERE account_id=? AND order_no=? AND ticker=? AND side=? "
        "AND created_at >= ? AND created_at < ? ORDER BY id DESC LIMIT 1",
        (account_id, f.order_no, f.ticker, f.side, lo, hi),
    ).fetchone()
    if row:
        return int(row["id"])
    # 응답을 못 받아 unknown(주문번호 없음)으로 남은 주문은 같은 날·종목·방향 체결로 확정한다 —
    # 단 **후보가 정확히 1건**일 때만(Phase 23 B). 둘 이상이면 어느 주문의 체결인지 알 수 없어
    # 붙이지 않고 알림 1회 + control 플래그(특보 orders 영역)로 사용자 확인에 맡긴다.
    rows = conn.execute(
        "SELECT id FROM orders WHERE account_id=? AND ticker=? AND side=? AND status='unknown' "
        "AND order_no IS NULL AND created_at >= ? AND created_at < ? ORDER BY id",
        (account_id, f.ticker, f.side, lo, hi),
    ).fetchall()
    if len(rows) > 1:
        _flag_ambiguous(conn, account_id, f, local_day, [int(r["id"]) for r in rows], notifier)
    row = rows[0] if len(rows) == 1 else None
    if row:
        conn.execute(
            "UPDATE orders SET order_no=?, status='accepted' WHERE id=?", (f.order_no, row["id"])
        )
        conn.commit()
        logger.info(
            "미확인 주문 확정 %s %s %s → 접수번호 %s", account_id, f.ticker, f.side, f.order_no
        )
        return int(row["id"])
    return repo.insert_order(
        conn,
        decision_id=None,
        account_id=account_id,
        ticker=f.ticker,
        market=f.market,
        side=f.side,
        qty=f.qty,
        order_type="LIMIT",
        price=f.price,
        status="accepted",
        order_no=f.order_no,
        message="원장 밖에서 낸 주문(체결 동기화로 생성)",
        mode="external",
        created_at=f.filled_at.isoformat(),
    )


def _refresh_status(conn: sqlite3.Connection, order_id: int) -> bool:
    o = conn.execute("SELECT qty, status FROM orders WHERE id=?", (order_id,)).fetchone()
    filled = conn.execute(
        "SELECT COALESCE(SUM(qty), 0) AS q FROM fills WHERE order_id=?", (order_id,)
    ).fetchone()["q"]
    status = "filled" if int(filled) >= int(o["qty"]) else "partial" if int(filled) > 0 else None
    if status is None or status == o["status"]:
        return False
    if status == "partial" and o["status"] in repo.DEAD_STATUSES:
        return False  # 마감에 잔량이 만료된 부분체결 주문을 다음 동기화가 다시 살리지 않는다
    conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
    conn.commit()
    return True


def _day_for(market: str, d: date | datetime) -> date:
    """datetime 이면 그 시장 현지 날짜(KIS 체결 조회의 주문일자는 현지 기준), date 면 그대로."""
    if isinstance(d, datetime):
        return d.astimezone(MARKET_TZ[market]).date()
    return d


def _session_closed(market: str, day: date, now: datetime) -> bool:
    """day 의 정규장이 끝났는가 (시장 현지 기준)."""
    local = now.astimezone(MARKET_TZ[market])
    if local.date() > day:
        return True
    return local.date() == day and local.time() >= market_close(market, day)


def expire_unfilled(conn: sqlite3.Connection, account_id: str, market: str, day: date) -> int:
    """day(현지)의 체결 없는 accepted·unknown 주문과 잔량이 남은 partial 주문을 expired 로
    (마감 뒤인지는 호출자가 본다). 당일 주문은 마감에 소멸하므로 partial 의 잔량도 죽었다 —
    살아 있는 상태로 두면 중복 방지 조건에 영구히 남는다(체결분은 fills 에 그대로)."""
    lo, hi = repo.day_bounds(day, market)
    cur = conn.execute(
        "UPDATE orders SET status='expired' WHERE account_id=? AND market=? "
        "AND created_at >= ? AND created_at < ? AND ("
        "(status IN ('accepted','unknown') AND id NOT IN (SELECT order_id FROM fills)) "
        "OR status='partial')",
        (account_id, market, lo, hi),
    )
    conn.commit()
    if cur.rowcount:
        logger.info("미체결 주문 %d건 만료 처리 %s %s %s", cur.rowcount, account_id, market, day)
    return cur.rowcount


def _flag_ambiguous(
    conn: sqlite3.Connection,
    account_id: str,
    f: Fill,
    local_day: date,
    order_ids: list[int],
    notifier: Any | None,
) -> None:
    from trader.control import control

    key = f"fill_ambiguous:{f.market}:{local_day.isoformat()}"
    cur = control.get_flag(conn, key) or {}
    cases = cur.get("cases") or []
    case = {"account_id": account_id, "ticker": f.ticker, "side": f.side, "order_ids": order_ids}
    if case in cases:
        return
    cases.append(case)
    control.set_flag(conn, key, {"cases": cases, "candidates": len(order_ids)})
    logger.warning(
        "체결 매칭 모호 %s %s %s: unknown 후보 %s", account_id, f.ticker, f.side, order_ids
    )
    if notifier is not None:
        try:
            notifier.send(
                f"[체결 매칭 모호] {account_id} {f.market} {f.ticker} {f.side}: 응답 없는 주문 "
                f"{len(order_ids)}건(#{', #'.join(str(i) for i in order_ids)}) 중 어느 것의 "
                "체결인지 알 수 없어 붙이지 않음 — KIS 앱에서 확인 뒤 정리"
            )
        except Exception:  # noqa: BLE001
            logger.debug("체결 매칭 모호 알림 실패", exc_info=True)


def sync_fills(
    conn: sqlite3.Connection,
    broker: Any,
    account: AccountConfig,
    d: date | datetime,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    notifier: Any | None = None,
) -> dict:
    """계좌의 시장별 체결을 적재한다. 같은 체결은 두 번 들어가지 않는다 (fills 유니크 인덱스).

    d 가 datetime 이면 시장마다 현지 날짜로 조회한다 (US 는 ET: 07:10 KST 동기화는 전날 ET).
    조회가 성공했고 그날 정규장이 끝났으면 체결 없는 accepted·unknown 주문을 expired 로 바꾼다.
    """
    inserted = 0
    replaced = 0
    expired = 0
    updated: set[int] = set()
    days = {m: _day_for(m, d) for m in account.markets}
    for market in account.markets:
        try:
            fills = broker.get_fills(market, days[market])
        except Exception as e:  # noqa: BLE001 - 한 시장 실패가 다른 시장을 막지 않는다
            logger.warning("체결 조회 실패 %s %s: %s", account.id, market, e)
            continue
        for f in fills:
            oid = _order_id_for(conn, account.id, f, notifier=notifier)
            # KIS 체결 조회는 주문당 누적 체결수량을 주므로 기존 행이 있으면 누적값으로 바꾼다
            existing = conn.execute(
                "SELECT COALESCE(SUM(qty), 0) AS q FROM fills WHERE order_id=?", (oid,)
            ).fetchone()["q"]
            if int(existing) == f.qty:
                _refresh_status(conn, oid)
                continue
            try:  # 교체(DELETE + INSERT)는 한 트랜잭션 — 중간에 끊기면 체결이 사라진다
                if int(existing) > 0:
                    conn.execute("DELETE FROM fills WHERE order_id=?", (oid,))
                    replaced += 1
                fid = repo.insert_fill(
                    conn,
                    order_id=oid,
                    account_id=account.id,
                    qty=f.qty,
                    price=Decimal(f.price),
                    filled_at=f.filled_at.isoformat(),
                )
            except sqlite3.Error:
                conn.rollback()
                raise
            if fid is not None:
                inserted += 1
            if _refresh_status(conn, oid):
                updated.add(oid)
        if _session_closed(market, days[market], now()):
            expired += expire_unfilled(conn, account.id, market, days[market])
    return {
        "account_id": account.id,
        "date": (
            {m: day.isoformat() for m, day in days.items()}
            if isinstance(d, datetime)
            else d.isoformat()
        ),
        "inserted": inserted - replaced,
        "updated": replaced,
        "orders_updated": len(updated),
        "expired": expired,
    }
