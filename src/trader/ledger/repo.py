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

from trader.config import KST, MARKET_TZ
from trader.ledger.db import now_iso

_ANALYST_RE = re.compile(r"^10_analyst_([a-z_]+)$")
_DECISION_RE = re.compile(r"^(30_decision|31_decision_r\d+)$")

# 살아 있지 않은 주문 상태 (중복 방지·당일 매수·주문금액·보유 폴백·차트 마커가 모두 이 목록을 쓴다).
# expired 는 체결 동기화가 마감 뒤 체결 없는 accepted·unknown 주문에 붙인다 (`fills.sync_fills`).
DEAD_STATUSES = ("rejected", "cancelled", "failed", "expired")


def dead_status_sql() -> str:
    """`status NOT IN (…)` 에 넣을 목록 문자열."""
    return ",".join(f"'{s}'" for s in DEAD_STATUSES)


_DEBATE_RE = re.compile(r"^20_debate_r(\d+)_(bull|bear)$")


def _kst_day_bounds(d: date) -> tuple[str, str]:
    return _day_bounds(d, KST)


def _day_bounds(d: date, tz: ZoneInfo) -> tuple[str, str]:
    """tz 의 날짜 d 하루를 UTC ISO 경계 [start, end) 로."""
    start = datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(UTC)
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


def day_bounds(d: date, market: str | None) -> tuple[str, str]:
    """시장 현지 날짜 d 하루의 UTC ISO 경계. 시장이 없거나 모르면 KST.

    US 는 KST 자정을 넘겨 장이 열리므로 KST 창으로 보면 같은 세션의 주문이 두 날에 갈린다."""
    return _day_bounds(d, MARKET_TZ[market]) if market in MARKET_TZ else _kst_day_bounds(d)


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


def insert_hedge_decision(
    conn: sqlite3.Connection, d: date, market: str, ticker: str, data: dict
) -> int:
    """파이썬이 만든 헤지 결정 행(source='hedge'). 같은 날·종목이면 갱신. 메모리·분포 제외."""
    conn.execute(
        "INSERT INTO decisions(date, market, ticker, action, target_weight, confidence, "
        "session_id, json, created_at, source) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, 'hedge') "
        "ON CONFLICT(date, market, ticker) DO UPDATE SET action=excluded.action, "
        "target_weight=excluded.target_weight, confidence=excluded.confidence, "
        "json=excluded.json, source='hedge'",
        (
            d.isoformat(),
            market,
            ticker,
            data.get("action"),
            data.get("target_weight"),
            data.get("confidence"),
            json.dumps(data, ensure_ascii=False, default=str),
            now_iso(),
        ),
    )
    conn.commit()
    did = decision_id(conn, d, market, ticker)
    assert did is not None
    return did


def _hydrate_decision(row: sqlite3.Row) -> dict:
    """decisions 행의 json 열을 data 로 복원 (get_decision(hydrate=True)·latest_decision 공통)."""
    d = dict(row)
    d["data"] = json.loads(d.pop("json"))
    return d


def get_decision(
    conn: sqlite3.Connection, decision_id: int, *, hydrate: bool = False
) -> dict | None:
    """결정 행. hydrate=True 면 json 열을 data 로 복원한다 (웹·모니터가 같은 모양을 쓴다)."""
    row = conn.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
    if row is None:
        return None
    return _hydrate_decision(row) if hydrate else dict(row)


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
    created_at: str | None = None,
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
            created_at or now_iso(),
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


def reevaluated_between(
    conn: sqlite3.Connection, market: str, ticker: str, kind: str, lo: str, hi: str
) -> bool:
    """[lo, hi) 안에 같은 종류의 트리거가 재평가 세션까지 마쳤는가 (운영일 1회 제한용).

    기동 실패(`session_id` 가 '실패:' 로 시작)는 세지 않아 30분 뒤 재시도할 수 있다."""
    row = conn.execute(
        "SELECT 1 FROM triggers WHERE market=? AND ticker=? AND kind=? "
        "AND handled_at >= ? AND handled_at < ? "
        "AND (session_id IS NULL OR session_id NOT LIKE '실패:%') LIMIT 1",
        (market, ticker, kind, lo, hi),
    ).fetchone()
    return row is not None


def last_trigger_handled_at(conn: sqlite3.Connection, market: str, ticker: str) -> datetime | None:
    """해당 종목의 마지막 재평가 시각 (30분 1회 제한용)."""
    row = conn.execute(
        "SELECT MAX(handled_at) AS t FROM triggers WHERE market=? AND ticker=? "
        "AND handled_at IS NOT NULL",
        (market, ticker),
    ).fetchone()
    return datetime.fromisoformat(row["t"]) if row and row["t"] else None


def has_open_order_or_approval(
    conn: sqlite3.Connection,
    decision_id: int,
    account_id: str,
    side: str | None = None,
    *,
    include_approvals: bool = True,
) -> bool:
    """같은 결정·계좌(·방향)에 살아 있는 주문(실패·거부·취소 제외)이나 pending 승인이 있으면 True.

    재평가(31_decision_r<n>)는 같은 decisions 행을 갱신하므로 방향이 다르면(BUY 뒤 SELL) 허용한다.
    include_approvals=False 면 주문만 본다 (손절은 아직 승인되지 않은 요청에 막히지 않아야 한다).
    """
    sql = (
        "SELECT 1 FROM orders WHERE decision_id=? AND account_id=? "
        f"AND status NOT IN ({dead_status_sql()})"
    )
    args: list = [decision_id, account_id]
    if side:
        sql += " AND side=?"
        args.append(side)
    if conn.execute(sql + " LIMIT 1", args).fetchone():
        return True
    if not include_approvals:
        return False
    sql = "SELECT 1 FROM approvals WHERE decision_id=? AND account_id=? AND status='pending'"
    args = [decision_id, account_id]
    if side:
        sql += " AND json_extract(order_json, '$.side')=?"
        args.append(side)
    return conn.execute(sql + " LIMIT 1", args).fetchone() is not None


def open_orders_today(
    conn: sqlite3.Connection,
    account_id: str,
    ticker: str,
    side: str,
    d: date,
    *,
    market: str | None = None,
) -> list[sqlite3.Row]:
    """오늘(시장 현지 하루) 같은 계좌·종목·방향의 살아 있고 체결이 끝나지 않은 주문 행
    (손절 전 취소 대상)."""
    lo, hi = day_bounds(d, market)
    return conn.execute(
        "SELECT id, order_no, org_no, qty, market, status, order_type, price FROM orders "
        "WHERE account_id=? AND ticker=? AND side=? "
        f"AND status NOT IN ({dead_status_sql()},'filled') "
        "AND created_at >= ? AND created_at < ? ORDER BY id",
        (account_id, ticker, side, lo, hi),
    ).fetchall()


def set_order_status(conn: sqlite3.Connection, order_id: int, status: str) -> None:
    conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
    conn.commit()


def has_open_order_today(
    conn: sqlite3.Connection,
    account_id: str,
    ticker: str,
    side: str,
    d: date,
    *,
    market: str | None = None,
    exclude_filled: bool = False,
) -> bool:
    """오늘 같은 계좌·종목·방향의 살아 있는 주문이 있으면 True (손절 반복 발주 방지).

    market 을 주면 d 를 그 시장의 현지 날짜로 보고 현지 하루 창을 쓴다 (US 모니터는 KST 자정을
    넘겨 돌므로 KST 창으로는 23:30 KST 주문을 00:30 KST 에 놓친다).
    exclude_filled=True 면 이미 체결이 끝난 주문은 세지 않는다 (오전에 체결된 TRIM 이 잔량의
    손절을 막으면 안 된다; 체결된 주문은 더 이상 "살아 있는" 주문이 아니다).
    """
    lo, hi = day_bounds(d, market)
    excluded = dead_status_sql() + (",'filled'" if exclude_filled else "")
    row = conn.execute(
        "SELECT 1 FROM orders WHERE account_id=? AND ticker=? AND side=? "
        f"AND status NOT IN ({excluded}) "
        "AND created_at >= ? AND created_at < ? LIMIT 1",
        (account_id, ticker, side, lo, hi),
    ).fetchone()
    return row is not None


def latest_trigger_price(
    conn: sqlite3.Connection, market: str, ticker: str, d: date
) -> Decimal | None:
    """운영일 d 의 가장 최근 트리거 가격 (재평가의 '현재가'). 없으면 None."""
    lo, hi = day_bounds(d, market)
    row = conn.execute(
        "SELECT price FROM triggers WHERE market=? AND ticker=? AND price IS NOT NULL "
        "AND created_at >= ? AND created_at < ? ORDER BY id DESC LIMIT 1",
        (market, ticker, lo, hi),
    ).fetchone()
    return _dec(row["price"]) if row else None


def get_trigger(conn: sqlite3.Connection, trigger_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, market, ticker, kind, detail, price, decision_id, handled_at FROM triggers "
        "WHERE id=?",
        (trigger_id,),
    ).fetchone()
    if row is None:
        return None
    out = dict(row)
    try:
        out["detail"] = json.loads(out["detail"]) if out.get("detail") else {}
    except ValueError:
        out["detail"] = {}
    return out


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


def latest_decision(conn: sqlite3.Connection, market: str, ticker: str) -> dict | None:
    """종목의 가장 최근 결정 행 (json → data). monitor 가 손절선·익절·무효화 조건을 읽는다."""
    row = conn.execute(
        "SELECT * FROM decisions WHERE market=? AND ticker=? ORDER BY date DESC, id DESC LIMIT 1",
        (market, ticker),
    ).fetchone()
    return _hydrate_decision(row) if row is not None else None


def today_buy_tickers(
    conn: sqlite3.Connection, account_id: str, d: date, market: str | None = None
) -> set[str]:
    """운영일 d(시장 현지 하루)에 낸 살아 있는 BUY 종목. 통화가 달라 market 으로도 거른다."""
    lo, hi = day_bounds(d, market)
    sql = (
        "SELECT DISTINCT ticker FROM orders WHERE account_id=? AND side IN ('BUY') "
        f"AND status NOT IN ({dead_status_sql()}) "
        "AND created_at >= ? AND created_at < ?"
    )
    args: list = [account_id, lo, hi]
    if market:
        sql += " AND market=?"
        args.append(market)
    return {r["ticker"] for r in conn.execute(sql, args).fetchall()}


def today_order_amount(
    conn: sqlite3.Connection,
    account_id: str,
    d: date,
    market: str | None = None,
    *,
    exclude_filled: bool = False,
) -> Decimal:
    """운영일 d(시장 현지 하루)의 누적 매수 주문금액. 통화가 달라 market 으로 거른다.
    exclude_filled=True 면 체결 완료 주문은 빼고 미체결(accepted·partial·unknown)만 더한다 —
    주문가능금액(D+2 정산 반영)이 체결분을 이미 뺐을 때 이중 차감을 막는다."""
    lo, hi = day_bounds(d, market)
    excluded = dead_status_sql() + (",'filled'" if exclude_filled else "")
    sql = (
        "SELECT qty, price, amount FROM orders WHERE account_id=? AND side='BUY' "
        f"AND status NOT IN ({excluded}) AND created_at >= ? AND created_at < ?"
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


def trades_this_week(
    conn: sqlite3.Connection, account_id: str, d: date, market: str | None = None
) -> dict[str, int]:
    """운영일 d 가 속한 주(월요일 ~ d, 시장 현지 하루 경계)의 종목별 살아 있는 주문 수."""
    monday = d - timedelta(days=d.weekday())
    lo, _ = day_bounds(monday, market)
    _, hi = day_bounds(d, market)
    sql = (
        "SELECT ticker, COUNT(*) AS n FROM orders WHERE account_id=? "
        f"AND status NOT IN ({dead_status_sql()}) "
        "AND created_at >= ? AND created_at < ?"
    )
    args: list = [account_id, lo, hi]
    if market:
        sql += " AND market=?"
        args.append(market)
    rows = conn.execute(sql + " GROUP BY ticker", args).fetchall()
    return {r["ticker"]: int(r["n"]) for r in rows}


def realized_pnl_5d(
    conn: sqlite3.Connection, account_id: str, d: date, market: str | None = None
) -> Decimal:
    """최근 5거래일(달력 7일 근사) 실현손익: 매도 체결금액 − **현재 보유 묶음의 평균 매수가**.

    창 안에 SELL 체결이 있는 종목만 전체 체결을 시간순으로 걸어 평균가를 갱신한다(보유 0 이면 리셋).
    전 이력 BUY 평균을 쓰면 전량 매도 뒤 재매수한 종목의 손익 부호가 뒤집혀 성향 판정이 틀린다.
    금액은 Decimal 로만 계산한다. 통화가 섞이지 않게 market 으로 거른다.
    """
    lo, _ = _kst_day_bounds(d - timedelta(days=7))
    _, hi = _kst_day_bounds(d)
    sql = (
        "SELECT DISTINCT o.ticker FROM fills f JOIN orders o ON o.id=f.order_id "
        "WHERE f.account_id=? AND o.side='SELL' AND f.filled_at >= ? AND f.filled_at < ?"
    )
    args: list = [account_id, lo, hi]
    if market:
        sql += " AND o.market=?"
        args.append(market)
    tickers = [r["ticker"] for r in conn.execute(sql, args).fetchall()]
    if not tickers:
        return Decimal(0)
    marks = ",".join("?" * len(tickers))
    sql = (
        "SELECT o.ticker, o.side, f.qty, f.price, f.filled_at FROM fills f "
        "JOIN orders o ON o.id=f.order_id "
        f"WHERE f.account_id=? AND o.ticker IN ({marks})"
    )
    args = [account_id, *tickers]
    if market:
        sql += " AND o.market=?"
        args.append(market)
    rows = conn.execute(sql + " ORDER BY f.filled_at, f.id", args).fetchall()
    total = Decimal(0)
    qty: dict[str, int] = {}
    cost: dict[str, Decimal] = {}
    for r in rows:
        t = r["ticker"]
        q = int(r["qty"])
        px = _dec(r["price"])
        if r["side"] == "BUY":
            qty[t] = qty.get(t, 0) + q
            cost[t] = cost.get(t, Decimal(0)) + px * q
            continue
        held = qty.get(t, 0)
        if held <= 0:
            # 매수 체결 기록이 없는 보유분(fills sync 이전·HTS 매수): 평단을 몰라 손익에 넣지 않는다
            # (평단 0 으로 치면 매도금액 전체가 이익이 돼 성향이 틀린다)
            continue
        avg = cost.get(t, Decimal(0)) / held
        if lo <= r["filled_at"] < hi:
            total += (px - avg) * q
        remaining = held - q
        if remaining <= 0:
            qty[t], cost[t] = 0, Decimal(0)
        else:
            qty[t], cost[t] = remaining, avg * remaining
    return total


def _first_buy_of_current_lot(
    events: list[tuple[str, str, int]], market: str | None = None
) -> date | None:
    """(시각, 방향, 수량) 시간순 이벤트에서 현재 보유 묶음의 첫 매수일. 보유가 0 이면 None.

    전량 매도 뒤 다시 사면 그 매수일부터 센다 (최소 보유기간이 재진입에도 적용되도록).
    날짜는 시장 현지(US 는 ET): KST 로 보면 ET 오전 체결이 다음 날이 돼 보유 거래일이 하루 준다."""
    tz = MARKET_TZ.get(market or "", KST)
    qty = 0
    since: date | None = None
    for at, side, n in events:
        if side == "BUY":
            if qty <= 0:
                since = datetime.fromisoformat(at).astimezone(tz).date()
                qty = 0
            qty += int(n)
        else:
            qty -= int(n)
            if qty <= 0:
                qty, since = 0, None
    return since if qty > 0 else None


def held_since(
    conn: sqlite3.Connection, account_id: str, tickers: list[str] | None = None
) -> dict[str, date]:
    """종목별 현재 보유 묶음의 첫 매수일 (최소 보유기간용). 전량 매도 뒤 재매수면 그 날부터.

    체결 기록이 있는 종목은 체결로, 없는 종목은 접수된 주문(매수 +, 매도 −)으로 계산한다.
    보유가 0 인 종목은 돌려주지 않는다. tickers 를 주면 그 종목만 읽는다 (판정마다 계좌 전체
    이력을 읽지 않도록 build_portfolio 가 브로커 보유 종목을 넘긴다; 결과는 같다).
    """
    if tickers is not None and not tickers:
        return {}
    scope, scope_args = "", []
    if tickers:
        scope = f" AND o.ticker IN ({','.join('?' * len(tickers))})"
        scope_args = list(tickers)
    by_ticker: dict[str, list[tuple[str, str, int]]] = {}
    markets: dict[str, str] = {}
    for r in conn.execute(
        "SELECT o.ticker, o.market, o.side, f.qty, f.filled_at FROM fills f "
        f"JOIN orders o ON o.id=f.order_id WHERE f.account_id=?{scope} ORDER BY f.filled_at, f.id",
        (account_id, *scope_args),
    ).fetchall():
        by_ticker.setdefault(r["ticker"], []).append((r["filled_at"], r["side"], r["qty"]))
        markets[r["ticker"]] = r["market"]
    out: dict[str, date] = {}
    for t, events in by_ticker.items():
        since = _first_buy_of_current_lot(events, markets.get(t))
        if since is not None:
            out[t] = since
    fallback: dict[str, list[tuple[str, str, int]]] = {}
    for r in conn.execute(
        "SELECT ticker, market, side, qty, created_at FROM orders o WHERE account_id=? "
        f"AND status NOT IN ({dead_status_sql()}){scope} ORDER BY created_at, id",
        (account_id, *scope_args),
    ).fetchall():
        if r["ticker"] in by_ticker:
            continue  # 체결 기록이 있는 종목은 체결이 정본
        fallback.setdefault(r["ticker"], []).append((r["created_at"], r["side"], r["qty"]))
        markets[r["ticker"]] = r["market"]
    for t, events in fallback.items():
        since = _first_buy_of_current_lot(events, markets.get(t))
        if since is not None:
            out[t] = since
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
    """계좌·날짜·종목 단위 upsert. nav·cash 는 시장(통화)별이라 _CASH 자리에도 market 을 둔다.

    같은 날 앞선 스냅샷에 있던 종목이 이번에 없으면(전량 매도) 그 행을 지운다 — 웹·트레이더 입력이
    유령 보유를 보지 않도록. 같은 시장의 행만 지운다 (통화·시장 분리)."""
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
    keep = [p["ticker"] for p in rows]
    marks = ",".join("?" * len(keep))
    conn.execute(
        "DELETE FROM positions_snapshot WHERE account_id=? AND date=? AND market IS ? "
        f"AND ticker NOT IN ({marks})",
        (account_id, d.isoformat(), market, *keep),
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
    if today is None:  # 스냅샷은 판정마다 종목 수만큼 쌓이므로 전 행을 읽지 않고 창만 SQL 로 자른다
        row = conn.execute(
            "SELECT MAX(date) AS d FROM positions_snapshot WHERE account_id=? AND market=? "
            "AND nav IS NOT NULL",
            (account_id, market),
        ).fetchone()
        if not row or not row["d"]:
            return []
        end = date.fromisoformat(row["d"])
    else:
        end = today
    lo = (end - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT date, nav FROM positions_snapshot WHERE account_id=? AND market=? "
        "AND nav IS NOT NULL AND date >= ? GROUP BY date HAVING id = MAX(id) ORDER BY date",
        (account_id, market, lo),
    ).fetchall()
    return [(r["date"], _dec(r["nav"])) for r in rows]


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


def close_stale_running(conn: sqlite3.Connection, *, older_than_iso: str) -> int:
    """older_than 이전에 시작해 아직 running 인 agent_calls 를 error 로 닫는다(재기동 정리)."""
    cur = conn.execute(
        "UPDATE agent_calls SET status='error', error='stale: 1시간 넘게 running' "
        "WHERE status='running' AND created_at < ?",
        (older_than_iso,),
    )
    conn.commit()
    return cur.rowcount


# --- 종목명 (Phase 16) ---------------------------------------------------------


def upsert_ticker_name(
    conn: sqlite3.Connection, market: str, ticker: str, name: str | None, source: str
) -> bool:
    """관측한 종목명을 적는다(최근 관측 우선). 빈 이름·코드와 같은 이름은 무시한다."""
    clean = (name or "").strip()
    if not clean or clean == ticker:
        return False
    conn.execute(
        "INSERT INTO ticker_names(market, ticker, name, source, updated_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(market, ticker) DO UPDATE SET name=excluded.name, source=excluded.source, "
        "updated_at=excluded.updated_at",
        (market, ticker, clean, source, now_iso()),
    )
    conn.commit()
    return True


def ticker_names(conn: sqlite3.Connection, market: str | None = None) -> dict[tuple[str, str], str]:
    sql = "SELECT market, ticker, name FROM ticker_names"
    params: tuple = ()
    if market is not None:
        sql += " WHERE market=?"
        params = (market,)
    return {(r["market"], r["ticker"]): r["name"] for r in conn.execute(sql, params)}
