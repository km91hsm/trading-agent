"""체결 동기화 (Phase 6): 브로커 체결 조회 → fills 멱등 적재 → orders.status 갱신.

주문번호가 원장에 없는 체결(앱·HTS 수동 주문)은 mode='external' 주문 행을 만들어 붙인다.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from trader.broker.models import Fill
from trader.config import AccountConfig
from trader.ledger import repo

logger = logging.getLogger("trader.fills")


def _order_id_for(conn: sqlite3.Connection, account_id: str, f: Fill) -> int:
    row = conn.execute(
        "SELECT id FROM orders WHERE account_id=? AND order_no=? ORDER BY id DESC LIMIT 1",
        (account_id, f.order_no),
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


def sync_fills(conn: sqlite3.Connection, broker: Any, account: AccountConfig, d: date) -> dict:
    """계좌의 시장별 체결을 적재한다. 같은 체결은 두 번 들어가지 않는다 (fills 유니크 인덱스)."""
    inserted = 0
    replaced = 0
    updated: set[int] = set()
    for market in account.markets:
        try:
            fills = broker.get_fills(market, d)
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
        "date": d.isoformat(),
        "inserted": inserted - replaced,
        "updated": replaced,
        "orders_updated": len(updated),
    }
