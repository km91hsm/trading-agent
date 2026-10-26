"""모의 체결 대 가정 주간 점검(Stock docs/07 「모의 운용 4-7」 교훈): 백테스트·게이트는 지정가가
체결되고 비용이 왕복 `costs.round_trip_bp` 라고 가정한다. 모의 계좌의 LLM 결정 주문으로 그 가정을
잰다 — 체결률(끝난 주문 중 체결)과 지정가 대비 체결가 차이(bp, +면 불리). 기준(체결률 80% 미만,
슬리피지가 편도 비용 가정의 2배 초과, 끝난 주문 `MIN_ORDERS` 이상일 때만)을 넘으면 경고 줄을
낸다. 읽기 전용(설정·주문 불변). 데몬 주간 잡 `paper_review`·CLI `trader paper-review`."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from decimal import Decimal

from trader.config import AccountConfig
from trader.ledger import repo
from trader.notify import messages
from trader.risk import costs

MIN_ORDERS = 5
MIN_FILL_RATE = 0.80
SLIP_MULT = 2.0
# partial 은 잔량이 살아 있는 진행 중 상태다(마감 뒤 잔량은 expired 로 바뀐다)
TERMINAL = ("filled", "expired", "cancelled")
OPEN = ("accepted", "unknown", "pending", "partial")


def _row(conn: sqlite3.Connection, account_id: str, market: str, lo: str, hi: str) -> dict:
    orders = conn.execute(
        "SELECT o.id, o.side, o.qty, o.price, o.status FROM orders o "
        "WHERE o.account_id=? AND o.market=? AND o.decision_id IS NOT NULL "
        "AND o.created_at >= ? AND o.created_at < ?",
        (account_id, market, lo, hi),
    ).fetchall()
    terminal = [o for o in orders if o["status"] in TERMINAL]
    n_open = sum(1 for o in orders if o["status"] in OPEN)
    filled, slips, qty_ordered, qty_filled = 0, [], 0, 0
    for o in terminal:
        qty_ordered += int(o["qty"] or 0)
        fills = conn.execute("SELECT qty, price FROM fills WHERE order_id=?", (o["id"],)).fetchall()
        qty = sum(int(f["qty"]) for f in fills)
        qty_filled += qty
        if qty <= 0:
            continue
        filled += 1
        ref = Decimal(str(o["price"])) if o["price"] not in (None, "") else Decimal(0)
        if ref <= 0:
            continue
        avg = sum(Decimal(str(f["price"])) * int(f["qty"]) for f in fills) / qty
        diff = (avg / ref - 1) if o["side"] == "BUY" else (1 - avg / ref)
        slips.append(float(diff) * 1e4)
    return {
        "account_id": account_id,
        "market": market,
        "terminal": len(terminal),
        "filled": filled,
        "open": n_open,
        "fill_rate": filled / len(terminal) if terminal else None,
        "qty_fill_rate": qty_filled / qty_ordered if qty_ordered else None,
        "slippage_bp": sum(slips) / len(slips) if slips else None,
        "assumed_bp": costs.round_trip_bp(market) // 2,
    }


def compute(
    conn: sqlite3.Connection, accounts: list[AccountConfig], *, since: date, until: date
) -> dict:
    """모의 계좌·시장별 [since, until) 점검. {"since","until","items","alerts"}."""
    items, alerts = [], []
    for a in accounts:
        if a.env != "paper":
            continue
        for m in a.markets:
            lo = repo.day_bounds(since, m)[0]
            hi = repo.day_bounds(until, m)[0]
            row = _row(conn, a.id, m, lo, hi)
            items.append(row)
            if row["terminal"] < MIN_ORDERS:
                continue
            if row["fill_rate"] is not None and row["fill_rate"] < MIN_FILL_RATE:
                alerts.append(
                    f"{a.id} {m} 체결률 {row['fill_rate']:.0%} — 지정가가 자주 안 맞아요"
                    f"(기준 {MIN_FILL_RATE:.0%}). 백테스트·게이트 가정보다 기회가 적어요"
                )
            slip = row["slippage_bp"]
            if slip is not None and slip > row["assumed_bp"] * SLIP_MULT:
                alerts.append(
                    f"{a.id} {m} 슬리피지 평균 {slip:+.1f}bp — 편도 비용 가정 "
                    f"{row['assumed_bp']}bp 의 {SLIP_MULT:.0f}배를 넘어요"
                )
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "items": items,
        "alerts": alerts,
    }


def weekly(conn: sqlite3.Connection, accounts: list[AccountConfig], *, today: date) -> dict:
    return compute(conn, accounts, since=today - timedelta(days=7), until=today)


def message(r: dict) -> str:
    lines = []
    for it in r["items"]:
        rate = "—" if it["fill_rate"] is None else f"{it['fill_rate']:.0%}"
        slip = "—" if it["slippage_bp"] is None else f"{it['slippage_bp']:+.1f}bp"
        lines.append(
            f"{it['account_id']} {it['market']}: 체결 {it['filled']}/{it['terminal']}({rate}) · "
            f"슬리피지 {slip}(가정 {it['assumed_bp']}bp) · 진행 중 {it['open']}"
        )
    lines += [f"주의: {a}" for a in r["alerts"]]
    return messages.event("모의 체결 점검", f"{r['since']} ~ {r['until']}", *lines)
