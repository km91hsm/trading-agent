"""원장 저장소: 체크포인트 적재(멱등), 주문·체결·포지션·판정·승인 기록과 리스크 규칙용 조회.

금액은 Decimal ↔ TEXT 로 저장한다. 날짜 비교는 UTC ISO 문자열(KST 하루 경계는 호출자가 계산).
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from trader.ledger.db import now_iso

_KST = ZoneInfo("Asia/Seoul")
_ANALYST_RE = re.compile(r"^10_analyst_([a-z_]+)$")
_DEBATE_RE = re.compile(r"^20_debate_r(\d+)_(bull|bear)$")


def _kst_day_bounds(d: date) -> tuple[str, str]:
    start = datetime(d.year, d.month, d.day, tzinfo=_KST).astimezone(UTC)
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


def _dec(v: Any) -> Decimal:
    return Decimal(str(v)) if v is not None and str(v) != "" else Decimal(0)


# --- 체크포인트 적재 ---------------------------------------------------------


def ingest_checkpoint(
    conn: sqlite3.Connection, d: date, market: str, ticker: str, stage: str, checkpoint: dict
) -> int | None:
    """10_/20_/30_ 체크포인트를 테이블에 멱등 저장하고 행 id 를 돌려준다. 그 외 단계는 None."""
    data = checkpoint.get("data") if isinstance(checkpoint, dict) else None
    if not isinstance(data, dict):
        return None
    call = checkpoint.get("call") or {}
    created = now_iso()
    js = json.dumps(data, ensure_ascii=False)
    m = _ANALYST_RE.match(stage)
    if m:
        conn.execute(
            "INSERT INTO analyst_reports(date, market, ticker, role, signal, score, confidence, "
            "json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(date, market, ticker, role) DO UPDATE SET signal=excluded.signal, "
            "score=excluded.score, confidence=excluded.confidence, json=excluded.json",
            (
                d.isoformat(),
                market,
                ticker,
                m.group(1),
                data.get("signal"),
                data.get("score"),
                data.get("confidence"),
                js,
                created,
            ),
        )
        conn.commit()
        return int(
            conn.execute(
                "SELECT id FROM analyst_reports WHERE date=? AND market=? AND ticker=? AND role=?",
                (d.isoformat(), market, ticker, m.group(1)),
            ).fetchone()["id"]
        )
    m = _DEBATE_RE.match(stage)
    if m:
        conn.execute(
            "INSERT INTO debate_turns(date, market, ticker, side, round, stance, conviction, "
            "json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(date, market, ticker, side, round) DO UPDATE SET stance=excluded.stance, "
            "conviction=excluded.conviction, json=excluded.json",
            (
                d.isoformat(),
                market,
                ticker,
                m.group(2),
                int(m.group(1)),
                data.get("stance"),
                data.get("conviction"),
                js,
                created,
            ),
        )
        conn.commit()
        return int(
            conn.execute(
                "SELECT id FROM debate_turns WHERE date=? AND market=? AND ticker=? AND side=? "
                "AND round=?",
                (d.isoformat(), market, ticker, m.group(2), int(m.group(1))),
            ).fetchone()["id"]
        )
    if stage == "30_decision":
        conn.execute(
            "INSERT INTO decisions(date, market, ticker, action, target_weight, confidence, "
            "session_id, json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(date, market, ticker) DO UPDATE SET action=excluded.action, "
            "target_weight=excluded.target_weight, confidence=excluded.confidence, "
            "session_id=excluded.session_id, json=excluded.json",
            (
                d.isoformat(),
                market,
                ticker,
                data.get("action"),
                data.get("target_weight"),
                data.get("confidence"),
                call.get("session_id"),
                js,
                created,
            ),
        )
        conn.commit()
        return decision_id(conn, d, market, ticker)
    return None


def get_decision(conn: sqlite3.Connection, decision_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
    return dict(row) if row else None


def decision_id(conn: sqlite3.Connection, d: date, market: str, ticker: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM decisions WHERE date=? AND market=? AND ticker=?",
        (d.isoformat(), market, ticker),
    ).fetchone()
    return int(row["id"]) if row else None


# --- 판정·주문·체결 -----------------------------------------------------------


def insert_risk_check(
    conn: sqlite3.Connection,
    *,
    decision_id: int,
    account_id: str,
    approved: bool,
    adjusted_qty: int | None,
    reasons: list[str],
) -> int:
    cur = conn.execute(
        "INSERT INTO risk_checks(decision_id, account_id, approved, adjusted_qty, reasons, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            decision_id,
            account_id,
            int(approved),
            adjusted_qty,
            json.dumps(reasons, ensure_ascii=False),
            now_iso(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def insert_order(
    conn: sqlite3.Connection,
    *,
    decision_id: int | None,
    account_id: str,
    ticker: str,
    market: str,
    side: str,
    qty: int,
    order_type: str,
    price: Decimal | None,
    status: str,
    order_no: str | None,
    message: str,
    mode: str,
    created_at: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO orders(decision_id, account_id, ticker, market, side, qty, order_type, price, "
        "status, order_no, message, mode, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            decision_id,
            account_id,
            ticker,
            market,
            side,
            qty,
            order_type,
            str(price) if price is not None else None,
            status,
            order_no,
            message,
            mode,
            created_at or now_iso(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def insert_fill(
    conn: sqlite3.Connection,
    *,
    order_id: int,
    account_id: str,
    qty: int,
    price: Decimal,
    filled_at: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO fills(order_id, account_id, qty, price, filled_at) VALUES (?, ?, ?, ?, ?)",
        (order_id, account_id, qty, str(price), filled_at or now_iso()),
    )
    conn.commit()
    return int(cur.lastrowid)


def today_buy_tickers(conn: sqlite3.Connection, account_id: str, d: date) -> set[str]:
    lo, hi = _kst_day_bounds(d)
    rows = conn.execute(
        "SELECT DISTINCT ticker FROM orders WHERE account_id=? AND side IN ('BUY') "
        "AND status NOT IN ('rejected','cancelled','failed') "
        "AND created_at >= ? AND created_at < ?",
        (account_id, lo, hi),
    ).fetchall()
    return {r["ticker"] for r in rows}


def today_order_amount(conn: sqlite3.Connection, account_id: str, d: date) -> Decimal:
    """당일 누적 매수 주문금액 (환전 API 없음 → 가능금액에서 차감)."""
    lo, hi = _kst_day_bounds(d)
    rows = conn.execute(
        "SELECT qty, price FROM orders WHERE account_id=? AND side='BUY' "
        "AND status NOT IN ('rejected','cancelled','failed') "
        "AND created_at >= ? AND created_at < ?",
        (account_id, lo, hi),
    ).fetchall()
    return sum((Decimal(r["qty"]) * _dec(r["price"]) for r in rows), Decimal(0))


def trades_this_week(conn: sqlite3.Connection, account_id: str, d: date) -> dict[str, int]:
    monday = d - timedelta(days=d.weekday())
    lo, _ = _kst_day_bounds(monday)
    _, hi = _kst_day_bounds(d)
    rows = conn.execute(
        "SELECT ticker, COUNT(*) AS n FROM orders WHERE account_id=? "
        "AND status NOT IN ('rejected','cancelled','failed') "
        "AND created_at >= ? AND created_at < ? "
        "GROUP BY ticker",
        (account_id, lo, hi),
    ).fetchall()
    return {r["ticker"]: int(r["n"]) for r in rows}


def realized_pnl_5d(conn: sqlite3.Connection, account_id: str, d: date) -> Decimal:
    """최근 5거래일(달력 7일 근사) 실현손익: 매도 체결금액 − 해당 수량의 평균 매수가."""
    lo, _ = _kst_day_bounds(d - timedelta(days=7))
    _, hi = _kst_day_bounds(d)
    sells = conn.execute(
        "SELECT o.ticker, f.qty, f.price FROM fills f JOIN orders o ON o.id=f.order_id "
        "WHERE f.account_id=? AND o.side='SELL' AND f.filled_at >= ? AND f.filled_at < ?",
        (account_id, lo, hi),
    ).fetchall()
    total = Decimal(0)
    for s in sells:
        buys = conn.execute(
            "SELECT f.qty, f.price FROM fills f JOIN orders o ON o.id=f.order_id "
            "WHERE f.account_id=? AND o.side='BUY' AND o.ticker=?",
            (account_id, s["ticker"]),
        ).fetchall()
        bq = sum(Decimal(b["qty"]) for b in buys)
        avg = (sum(Decimal(b["qty"]) * _dec(b["price"]) for b in buys) / bq) if bq else Decimal(0)
        total += Decimal(s["qty"]) * (_dec(s["price"]) - avg)
    return total


def held_since(conn: sqlite3.Connection, account_id: str) -> dict[str, date]:
    """종목별 최초 매수 체결일 (최소 보유기간 계산용)."""
    rows = conn.execute(
        "SELECT o.ticker, MIN(f.filled_at) AS first FROM fills f JOIN orders o ON o.id=f.order_id "
        "WHERE f.account_id=? AND o.side='BUY' GROUP BY o.ticker",
        (account_id,),
    ).fetchall()
    return {r["ticker"]: datetime.fromisoformat(r["first"]).astimezone(_KST).date() for r in rows}


def snapshot_positions(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    d: date,
    nav: Decimal,
    cash: Decimal,
    positions: list[dict],
) -> None:
    created = now_iso()
    rows = positions or [
        {
            "ticker": "_CASH",
            "market": None,
            "qty": 0,
            "avg_price": None,
            "current_price": None,
            "eval_amount": None,
        }
    ]
    for p in rows:
        conn.execute(
            "INSERT INTO positions_snapshot(account_id, date, ticker, market, qty, avg_price, "
            "current_price, eval_amount, nav, cash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(account_id, date, ticker) DO UPDATE SET qty=excluded.qty, "
            "avg_price=excluded.avg_price, current_price=excluded.current_price, "
            "eval_amount=excluded.eval_amount, nav=excluded.nav, cash=excluded.cash",
            (
                account_id,
                d.isoformat(),
                p["ticker"],
                p.get("market"),
                int(p.get("qty", 0)),
                _s(p.get("avg_price")),
                _s(p.get("current_price")),
                _s(p.get("eval_amount")),
                str(nav),
                str(cash),
                created,
            ),
        )
    conn.commit()


def latest_nav_before(conn: sqlite3.Connection, account_id: str, d: date) -> Decimal | None:
    """d 이전 마지막 스냅샷의 NAV (일일 손익 계산 기준). 없으면 None."""
    row = conn.execute(
        "SELECT nav FROM positions_snapshot WHERE account_id=? AND date < ? "
        "ORDER BY date DESC LIMIT 1",
        (account_id, d.isoformat()),
    ).fetchone()
    return _dec(row["nav"]) if row and row["nav"] is not None else None


def _s(v: Any) -> str | None:
    return None if v is None else str(v)


# --- 승인 ---------------------------------------------------------------------


def create_approval(
    conn: sqlite3.Connection,
    *,
    decision_id: int,
    account_id: str,
    order: dict,
    requested_at: str,
    expires_at: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO approvals(decision_id, account_id, order_json, status, requested_at, "
        "expires_at) VALUES (?, ?, ?, 'pending', ?, ?)",
        (decision_id, account_id, json.dumps(order, ensure_ascii=False), requested_at, expires_at),
    )
    conn.commit()
    return int(cur.lastrowid)


def _approval_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["order"] = json.loads(d.pop("order_json"))
    return d


def get_approval(conn: sqlite3.Connection, approval_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
    return _approval_row(r) if r else None


def pending_approvals(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM approvals WHERE status='pending' ORDER BY id").fetchall()
    return [_approval_row(r) for r in rows]


def decide_approval(
    conn: sqlite3.Connection, approval_id: int, status: str, *, decided_at: str | None = None
) -> None:
    conn.execute(
        "UPDATE approvals SET status=?, decided_at=? WHERE id=?",
        (status, decided_at or now_iso(), approval_id),
    )
    conn.commit()
