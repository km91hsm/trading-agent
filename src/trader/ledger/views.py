"""웹 UI 용 읽기 전용 조회 (스펙 §8). 계좌 단위 조회는 전부 account_id 로 거른다.

반환은 JSON 직렬화 가능한 dict 목록이며 금액은 TEXT(Decimal 문자열) 그대로 둔다.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from trader.ledger import metrics, repo

_KST = ZoneInfo("Asia/Seoul")
MARKETS = ("KR", "US")


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def list_orders(conn: sqlite3.Connection, account_id: str, *, limit: int = 100) -> list[dict]:
    return _rows(
        conn.execute(
            "SELECT * FROM orders WHERE account_id=? ORDER BY id DESC LIMIT ?", (account_id, limit)
        )
    )


def list_approvals(
    conn: sqlite3.Connection, account_id: str, *, status: str | None = None
) -> list[dict]:
    sql = "SELECT * FROM approvals WHERE account_id=?"
    args: list = [account_id]
    if status:
        sql += " AND status=?"
        args.append(status)
    out = []
    for r in conn.execute(sql + " ORDER BY id DESC", args).fetchall():
        d = dict(r)
        d["order"] = json.loads(d.pop("order_json"))
        out.append(d)
    return out


def list_risk_checks(conn: sqlite3.Connection, account_id: str, *, limit: int = 100) -> list[dict]:
    out = []
    for r in conn.execute(
        "SELECT rc.*, d.ticker, d.market, d.date, d.action FROM risk_checks rc "
        "LEFT JOIN decisions d ON d.id=rc.decision_id WHERE rc.account_id=? "
        "ORDER BY rc.id DESC LIMIT ?",
        (account_id, limit),
    ).fetchall():
        d = dict(r)
        d["approved"] = bool(d["approved"])
        d["reasons"] = json.loads(d["reasons"] or "[]")
        out.append(d)
    return out


def latest_positions(conn: sqlite3.Connection, account_id: str, market: str) -> dict | None:
    """해당 계좌·시장의 최신 스냅샷 날짜 포지션 (현금 자리 행 제외)."""
    row = conn.execute(
        "SELECT date, nav, cash FROM positions_snapshot WHERE account_id=? AND market=? "
        "ORDER BY date DESC, id DESC LIMIT 1",
        (account_id, market),
    ).fetchone()
    if row is None:
        return None
    positions = _rows(
        conn.execute(
            "SELECT ticker, market, qty, avg_price, current_price, eval_amount "
            "FROM positions_snapshot WHERE account_id=? AND market=? AND date=? "
            "AND ticker NOT LIKE '\\_CASH%' ESCAPE '\\' ORDER BY ticker",
            (account_id, market, row["date"]),
        )
    )
    return {"date": row["date"], "nav": row["nav"], "cash": row["cash"], "positions": positions}


def list_decisions(
    conn: sqlite3.Connection,
    *,
    days: int = 30,
    market: str | None = None,
    now: datetime | None = None,
) -> list[dict]:
    since = ((now or datetime.now(UTC)).astimezone(_KST).date() - timedelta(days=days)).isoformat()
    sql = (
        "SELECT id, date, market, ticker, action, target_weight, confidence, session_id, "
        "created_at FROM decisions WHERE date >= ?"
    )
    args: list = [since]
    if market:
        sql += " AND market=?"
        args.append(market)
    return _rows(conn.execute(sql + " ORDER BY date DESC, id DESC", args))


def get_decision(conn: sqlite3.Connection, decision_id: int) -> dict | None:
    d = repo.get_decision(conn, decision_id)
    if d is None:
        return None
    d["data"] = json.loads(d.pop("json"))
    return d


def decision_outcomes(conn: sqlite3.Connection, decision_id: int) -> list[dict]:
    """계좌별 판정 + (있으면) 마지막 주문 상태."""
    out = []
    for r in conn.execute(
        "SELECT account_id, approved, adjusted_qty, reasons, created_at FROM risk_checks "
        "WHERE decision_id=? ORDER BY id",
        (decision_id,),
    ).fetchall():
        o = conn.execute(
            "SELECT status, order_no, qty, price, mode FROM orders WHERE decision_id=? "
            "AND account_id=? ORDER BY id DESC LIMIT 1",
            (decision_id, r["account_id"]),
        ).fetchone()
        out.append(
            {
                "account_id": r["account_id"],
                "approved": bool(r["approved"]),
                "adjusted_qty": r["adjusted_qty"],
                "reasons": json.loads(r["reasons"] or "[]"),
                "checked_at": r["created_at"],
                "order_status": o["status"] if o else None,
                "order_no": o["order_no"] if o else None,
                "order_qty": o["qty"] if o else None,
                "order_price": o["price"] if o else None,
                "order_mode": o["mode"] if o else None,
            }
        )
    return out


def list_runs(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict]:
    return _rows(conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)))


def list_agent_calls(
    conn: sqlite3.Connection, *, days: int = 7, now: datetime | None = None
) -> list[dict]:
    since = ((now or datetime.now(UTC)) - timedelta(days=days)).isoformat()
    return _rows(
        conn.execute("SELECT * FROM agent_calls WHERE created_at >= ? ORDER BY id DESC", (since,))
    )


def account_summary(conn: sqlite3.Connection, account_id: str, today: date) -> dict:
    nav_by_market: dict[str, str | None] = {}
    for m in MARKETS:
        snap = conn.execute(
            "SELECT nav FROM positions_snapshot WHERE account_id=? AND market=? "
            "ORDER BY date DESC, id DESC LIMIT 1",
            (account_id, m),
        ).fetchone()
        nav_by_market[m] = snap["nav"] if snap else None
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM approvals WHERE account_id=? AND status='pending'",
        (account_id,),
    ).fetchone()["n"]
    return {
        "stance_by_market": metrics.stance_by_market(conn, account_id, today, MARKETS),
        "nav_by_market": nav_by_market,
        "pending_approvals": int(pending),
    }
