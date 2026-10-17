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
_DECISION_RE = re.compile(r"^(30_decision|31_decision_r\d+)$")
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
    if _DECISION_RE.match(stage):
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
    amount: Decimal | None = None,
    org_no: str | None = None,
) -> int:
    """amount 는 가능금액 차감용 주문금액(시장가는 판정 때 현재가 × 수량). 없으면 qty × price."""
    if amount is None and price is not None:
        amount = Decimal(qty) * price
    cur = conn.execute(
        "INSERT INTO orders(decision_id, account_id, ticker, market, side, qty, order_type, price, "
        "status, order_no, message, mode, created_at, amount, org_no) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            str(amount) if amount is not None else None,
            org_no,
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
) -> int | None:
    """체결 1건 적재. 같은 (계좌, 주문, 시각, 수량, 가격) 은 무시하고 None (재동기화 멱등)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO fills(order_id, account_id, qty, price, filled_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (order_id, account_id, qty, str(price), filled_at or now_iso()),
    )
    conn.commit()
    return int(cur.lastrowid) if cur.rowcount == 1 else None


# --- 트리거 (Phase 6 monitor) ------------------------------------------------------


def insert_trigger(
    conn: sqlite3.Connection,
    *,
    market: str,
    ticker: str,
    kind: str,
    detail: dict,
    price: Decimal | None,
    decision_id: int | None,
    account_id: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO triggers(account_id, market, ticker, kind, detail, price, decision_id, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            account_id,
            market,
            ticker,
            kind,
            json.dumps(detail, ensure_ascii=False, default=str),
            str(price) if price is not None else None,
            decision_id,
            now_iso(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def mark_trigger_handled(
    conn: sqlite3.Connection,
    trigger_id: int,
    session_id: str | None,
    *,
    handled_at: str | None = None,
) -> None:
    conn.execute(
        "UPDATE triggers SET handled_at=?, session_id=? WHERE id=?",
        (handled_at or now_iso(), session_id, trigger_id),
    )
    conn.commit()


def last_trigger_handled_at(conn: sqlite3.Connection, market: str, ticker: str) -> datetime | None:
    """해당 종목의 마지막 재평가 시각 (30분 1회 제한용)."""
    row = conn.execute(
        "SELECT MAX(handled_at) AS t FROM triggers WHERE market=? AND ticker=? "
        "AND handled_at IS NOT NULL",
        (market, ticker),
    ).fetchone()
    return datetime.fromisoformat(row["t"]) if row and row["t"] else None


def has_open_order_or_approval(
    conn: sqlite3.Connection, decision_id: int, account_id: str, side: str | None = None
) -> bool:
    """같은 결정·계좌(·방향)에 살아 있는 주문(실패·거부·취소 제외)이나 pending 승인이 있으면 True.

    재평가(31_decision_r<n>)는 같은 decisions 행을 갱신하므로 방향이 다르면(BUY 뒤 SELL) 허용한다.
    """
    sql = (
        "SELECT 1 FROM orders WHERE decision_id=? AND account_id=? "
        "AND status NOT IN ('rejected','cancelled','failed')"
    )
    args: list = [decision_id, account_id]
    if side:
        sql += " AND side=?"
        args.append(side)
    if conn.execute(sql + " LIMIT 1", args).fetchone():
        return True
    sql = "SELECT 1 FROM approvals WHERE decision_id=? AND account_id=? AND status='pending'"
    args = [decision_id, account_id]
    if side:
        sql += " AND json_extract(order_json, '$.side')=?"
        args.append(side)
    return conn.execute(sql + " LIMIT 1", args).fetchone() is not None


def has_open_order_today(
    conn: sqlite3.Connection, account_id: str, ticker: str, side: str, d: date
) -> bool:
    """오늘 같은 계좌·종목·방향의 살아 있는 주문이 있으면 True (손절 반복 발주 방지)."""
    lo, hi = _kst_day_bounds(d)
    row = conn.execute(
        "SELECT 1 FROM orders WHERE account_id=? AND ticker=? AND side=? "
        "AND status NOT IN ('rejected','cancelled','failed') "
        "AND created_at >= ? AND created_at < ? LIMIT 1",
        (account_id, ticker, side, lo, hi),
    ).fetchone()
    return row is not None


def unhandled_trigger_id(
    conn: sqlite3.Connection, market: str, ticker: str, kind: str
) -> int | None:
    """같은 종목·종류의 미처리 트리거 id (있으면 다시 쌓지 않고 그 행을 재평가에 연결한다)."""
    row = conn.execute(
        "SELECT id FROM triggers WHERE market=? AND ticker=? AND kind=? AND handled_at IS NULL "
        "ORDER BY id LIMIT 1",
        (market, ticker, kind),
    ).fetchone()
    return int(row["id"]) if row else None


def has_unhandled_trigger(conn: sqlite3.Connection, market: str, ticker: str, kind: str) -> bool:
    return unhandled_trigger_id(conn, market, ticker, kind) is not None


def latest_decision(conn: sqlite3.Connection, market: str, ticker: str) -> dict | None:
    """종목의 가장 최근 결정 행 (json → data). monitor 가 손절선·익절·무효화 조건을 읽는다."""
    row = conn.execute(
        "SELECT * FROM decisions WHERE market=? AND ticker=? ORDER BY date DESC, id DESC LIMIT 1",
        (market, ticker),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["data"] = json.loads(d.pop("json"))
    return d


def today_buy_tickers(conn: sqlite3.Connection, account_id: str, d: date) -> set[str]:
    lo, hi = _kst_day_bounds(d)
    rows = conn.execute(
        "SELECT DISTINCT ticker FROM orders WHERE account_id=? AND side IN ('BUY') "
        "AND status NOT IN ('rejected','cancelled','failed') "
        "AND created_at >= ? AND created_at < ?",
        (account_id, lo, hi),
    ).fetchall()
    return {r["ticker"] for r in rows}


def today_order_amount(
    conn: sqlite3.Connection, account_id: str, d: date, market: str | None = None
) -> Decimal:
    """당일 누적 매수 주문금액 (가능금액 차감용). 통화가 달라 market 으로 거른다."""
    lo, hi = _kst_day_bounds(d)
    sql = (
        "SELECT qty, price, amount FROM orders WHERE account_id=? AND side='BUY' "
        "AND status NOT IN ('rejected','cancelled','failed') AND created_at >= ? AND created_at < ?"
    )
    args: list = [account_id, lo, hi]
    if market:
        sql += " AND market=?"
        args.append(market)
    total = Decimal(0)
    for r in conn.execute(sql, args).fetchall():
        if r["amount"] is not None:
            total += _dec(r["amount"])
        else:
            total += Decimal(r["qty"]) * _dec(r["price"])
    return total


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


def realized_pnl_5d(
    conn: sqlite3.Connection, account_id: str, d: date, market: str | None = None
) -> Decimal:
    """최근 5거래일(달력 7일 근사) 실현손익: 매도 체결금액 − 해당 수량의 평균 매수가.

    fills 는 Phase 6 체결 동기화 전까지 비어 있어 0 이다. 통화가 섞이지 않게 market 으로 거른다.
    """
    lo, _ = _kst_day_bounds(d - timedelta(days=7))
    _, hi = _kst_day_bounds(d)
    sql = (
        "SELECT o.ticker, f.qty, f.price FROM fills f JOIN orders o ON o.id=f.order_id "
        "WHERE f.account_id=? AND o.side='SELL' AND f.filled_at >= ? AND f.filled_at < ?"
    )
    args: list = [account_id, lo, hi]
    if market:
        sql += " AND o.market=?"
        args.append(market)
    sells = conn.execute(sql, args).fetchall()
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
    """종목별 최초 매수일 (최소 보유기간용). 체결 기록이 없으면 접수된 매수 주문일로 대신한다."""
    rows = conn.execute(
        "SELECT o.ticker, MIN(f.filled_at) AS first FROM fills f JOIN orders o ON o.id=f.order_id "
        "WHERE f.account_id=? AND o.side='BUY' GROUP BY o.ticker",
        (account_id,),
    ).fetchall()
    out = {r["ticker"]: datetime.fromisoformat(r["first"]).astimezone(_KST).date() for r in rows}
    rows = conn.execute(
        "SELECT ticker, MIN(created_at) AS first FROM orders WHERE account_id=? AND side='BUY' "
        "AND status='accepted' GROUP BY ticker",
        (account_id,),
    ).fetchall()
    for r in rows:
        out.setdefault(r["ticker"], datetime.fromisoformat(r["first"]).astimezone(_KST).date())
    return out


def snapshot_positions(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    d: date,
    nav: Decimal,
    cash: Decimal,
    positions: list[dict],
    market: str | None = None,
) -> None:
    """계좌·날짜·종목 단위 upsert. nav·cash 는 시장(통화)별이라 _CASH 자리에도 market 을 둔다."""
    created = now_iso()
    rows = positions or [
        {
            "ticker": f"_CASH_{market}" if market else "_CASH",
            "market": market,
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
                p.get("market") or market,
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


def nav_history(
    conn: sqlite3.Connection,
    account_id: str,
    market: str,
    *,
    days: int = 30,
    today: date | None = None,
) -> list[tuple[str, Decimal]]:
    """(today 또는 마지막 스냅샷 날짜) − days 이후의 (날짜, NAV) 오름차순 (날짜별 최신 스냅샷).

    스냅샷 개수가 아니라 날짜 창이다: 운영이 띄엄띄엄이어도 오래된 값이 섞이지 않는다.
    """
    rows = conn.execute(
        "SELECT date, nav FROM positions_snapshot WHERE account_id=? AND market=? "
        "AND nav IS NOT NULL ORDER BY date, id",
        (account_id, market),
    ).fetchall()
    by_date: dict[str, Decimal] = {}
    for r in rows:
        by_date[r["date"]] = _dec(r["nav"])
    if not by_date:
        return []
    end = today or date.fromisoformat(max(by_date))
    lo = (end - timedelta(days=days)).isoformat()
    return [(d, by_date[d]) for d in sorted(by_date) if d >= lo]


def latest_nav_before(
    conn: sqlite3.Connection, account_id: str, d: date, market: str | None = None
) -> Decimal | None:
    """d 이전 마지막 스냅샷의 NAV (일일 손익 기준). 시장별 통화가 달라 market 으로 거른다."""
    sql = "SELECT nav FROM positions_snapshot WHERE account_id=? AND date < ?"
    args: list = [account_id, d.isoformat()]
    if market:
        sql += " AND market=?"
        args.append(market)
    row = conn.execute(sql + " ORDER BY date DESC, id DESC LIMIT 1", args).fetchone()
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
    conn: sqlite3.Connection,
    approval_id: int,
    status: str,
    *,
    decided_at: str | None = None,
    expect: str = "pending",
) -> bool:
    """`expect` 상태일 때만 바꾼다(조건부 UPDATE). 다른 프로세스가 먼저 처리했으면 False."""
    cur = conn.execute(
        "UPDATE approvals SET status=?, decided_at=? WHERE id=? AND status=?",
        (status, decided_at or now_iso(), approval_id, expect),
    )
    conn.commit()
    return cur.rowcount == 1
