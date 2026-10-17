"""체결 동기화 (Phase 6): 브로커 체결 조회 → fills 멱등 적재 → orders.status 갱신.

주문번호가 원장에 없는 체결(앱·HTS 수동 주문)은 mode='external' 주문 행을 만들어 붙인다.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from trader.broker.models import Fill
from trader.config import MARKET_TZ, AccountConfig
from trader.ledger import repo

logger = logging.getLogger("trader.fills")


def _order_id_for(conn: sqlite3.Connection, account_id: str, f: Fill) -> int:
    # KIS 주문번호(ODNO)는 날마다 다시 시작하므로 종목·방향까지 같아야 같은 주문으로 본다
    row = conn.execute(
        "SELECT id FROM orders WHERE account_id=? AND order_no=? AND ticker=? AND side=? "
        "ORDER BY id DESC LIMIT 1",
        (account_id, f.order_no, f.ticker, f.side),
    ).fetchone()
    if row:
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
    conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
    conn.commit()
    return True


def _day_for(market: str, d: date | datetime) -> date:
    """datetime 이면 그 시장 현지 날짜(KIS 체결 조회의 주문일자는 현지 기준), date 면 그대로."""
    if isinstance(d, datetime):
        return d.astimezone(MARKET_TZ[market]).date()
    return d


def sync_fills(
    conn: sqlite3.Connection, broker: Any, account: AccountConfig, d: date | datetime
) -> dict:
    """계좌의 시장별 체결을 적재한다. 같은 체결은 두 번 들어가지 않는다 (fills 유니크 인덱스).

    d 가 datetime 이면 시장마다 현지 날짜로 조회한다 (US 는 ET: 07:10 KST 동기화는 전날 ET).
    """
    inserted = 0
    replaced = 0
    updated: set[int] = set()
    days = {m: _day_for(m, d) for m in account.markets}
    for market in account.markets:
        try:
            fills = broker.get_fills(market, days[market])
        except Exception as e:  # noqa: BLE001 - 한 시장 실패가 다른 시장을 막지 않는다
            logger.warning("체결 조회 실패 %s %s: %s", account.id, market, e)
            continue
        for f in fills:
            oid = _order_id_for(conn, account.id, f)
            # KIS 체결 조회는 주문당 누적 체결수량을 주므로 기존 행이 있으면 누적값으로 바꾼다
            existing = conn.execute(
                "SELECT COALESCE(SUM(qty), 0) AS q FROM fills WHERE order_id=?", (oid,)
            ).fetchone()["q"]
            if int(existing) == f.qty:
                _refresh_status(conn, oid)
                continue
            if int(existing) > 0:
                conn.execute("DELETE FROM fills WHERE order_id=?", (oid,))
                conn.commit()
                replaced += 1
            fid = repo.insert_fill(
                conn,
                order_id=oid,
                account_id=account.id,
                qty=f.qty,
                price=Decimal(f.price),
                filled_at=f.filled_at.isoformat(),
            )
            if fid is not None:
                inserted += 1
            if _refresh_status(conn, oid):
                updated.add(oid)
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
    }
