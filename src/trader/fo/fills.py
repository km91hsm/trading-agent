"""파생 체결 동기화(Phase 26 단계 2 Task 2.7, 스펙 §5 R15). `orchestrator/fills.sync_fills` 가
`account.asset == "fo"` 면 여기로 위임한다.

- 체결은 **원주문 계보**(`orgn_odno` 또는 `odno`) 단위로 누적값(`filled_qty`·`filled_amount`)을
  읽고 `fills` 행 1개를 **교체**한다(현물과 같은 방식, 차분 삽입 아님). 증분 평균가는
  `Δ금액 ÷ Δ수량` 으로만 계산해 포지션 평균가·실현손실·비용에 쓴다.
- 다리 매칭은 `order_no`(계보 원주문) → 같은 계좌의 `fo_order_legs.order_no`. 주문번호 없는
  `sending|unknown` 다리는 같은 코드·방향·수량 후보가 **정확히 1건**일 때만 묶는다. 어느 다리에도
  붙지 않는 체결은 외부 주문(`orders(asset='fo', mode='external')`)으로 적재한다.
- 만료는 **조회 성공 ∧ 파생 세션 종료 뒤**에만: 그날 체결 없는 `accepted` 다리·주문을 `expired`.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from trader.broker.models import FoFill
from trader.config import KST, AccountConfig, FoCfg
from trader.fo import store
from trader.fo.schemas import order_side
from trader.fo.types import FoLegRow, is_entry
from trader.ledger import repo
from trader.market_hours import fo_close_hm

logger = logging.getLogger("trader.fo.fills")


def _day(d: date | datetime) -> date:
    return d.astimezone(KST).date() if isinstance(d, datetime) else d


def _lineages(fills: list[FoFill]) -> dict[str, dict[str, Any]]:
    """원주문 번호 → {code, side, filled, amount, total, filled_at}. 계보 안 행들의 누적 최대."""
    out: dict[str, dict[str, Any]] = {}
    for f in fills:
        key = f.orgn_order_no or f.order_no
        cur = out.setdefault(
            key,
            {"code": f.code, "side": f.side, "filled": 0, "amount": Decimal(0), "total": 0,
             "filled_at": f.filled_at},
        )  # fmt: skip
        cur["filled"] = max(cur["filled"], f.filled_qty)
        cur["amount"] = max(cur["amount"], f.filled_amount)
        cur["total"] = max(cur["total"], f.filled_qty + f.remaining_qty + f.rejected_qty)
        cur["filled_at"] = max(cur["filled_at"], f.filled_at)
    return out


def _leg_for(
    conn: sqlite3.Connection, account_id: str, order_no: str, lin: dict[str, Any]
) -> tuple[FoLegRow | None, bool]:
    """(다리, 이번에 새로 묶었는가)."""
    row = conn.execute(
        "SELECT id FROM fo_order_legs WHERE account_id = ? AND order_no = ? "
        "ORDER BY id DESC LIMIT 1",
        (account_id, order_no),
    ).fetchone()
    if row:
        return store.get_leg(conn, int(row["id"])), False
    bound = {
        r["order_no"]
        for r in conn.execute(
            "SELECT order_no FROM fo_order_legs WHERE order_no IS NOT NULL AND account_id = ?",
            (account_id,),
        )
    }
    if order_no in bound:
        return None, False
    cands = []
    for leg in store.live_legs(conn, account_id=account_id):
        if leg.status not in ("sending", "unknown") or leg.order_no is not None:
            continue
        pos = store.get_position(conn, leg.position_id)
        if pos is None or pos.code != lin["code"] or leg.qty != lin["total"]:
            continue
        if order_side(pos.contract_side, entry=is_entry(leg.intent)) != lin["side"]:
            continue
        cands.append(leg)
    if len(cands) != 1:
        return None, False
    leg = cands[0]
    store.set_leg(conn, leg.id, order_no=order_no, status="accepted")
    logger.info("파생 미확인 다리 %s 확정 → 접수번호 %s", leg.id, order_no)
    return store.get_leg(conn, leg.id), True


def _replace_fill(
    conn: sqlite3.Connection, *, order_id: int, account_id: str, filled: int, amount: Decimal,
    filled_at: datetime,
) -> bool:  # fmt: skip
    """계보당 fills 행 1개를 누적값으로 교체. 바뀐 것이 없으면 False."""
    existing = conn.execute(
        "SELECT COALESCE(SUM(qty), 0) AS q FROM fills WHERE order_id = ?", (order_id,)
    ).fetchone()["q"]
    if int(existing) == filled:
        return False
    price = (amount / filled) if filled else Decimal(0)
    conn.execute("DELETE FROM fills WHERE order_id = ?", (order_id,))
    repo.insert_fill(
        conn, order_id=order_id, account_id=account_id, qty=filled, price=price,
        filled_at=filled_at.isoformat(),
    )  # fmt: skip
    return True


def _order_status(conn: sqlite3.Connection, order_id: int, filled: int) -> None:
    o = conn.execute("SELECT qty, status FROM orders WHERE id = ?", (order_id,)).fetchone()
    if o is None:
        return
    status = "filled" if filled >= int(o["qty"]) else "partial" if filled > 0 else None
    if status and status != o["status"] and o["status"] not in repo.DEAD_STATUSES:
        conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))


def sync(
    conn: sqlite3.Connection,
    broker: Any,
    account: AccountConfig,
    d: date | datetime,
    *,
    cfg: FoCfg,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    notifier: Any | None = None,
) -> dict:
    day = _day(d)
    out: dict[str, Any] = {
        "account_id": account.id, "date": day.isoformat(), "inserted": 0, "updated": 0,
        "external": 0, "bound": 0, "expired": 0, "failed_markets": [],
    }  # fmt: skip
    try:
        fills = broker.get_fills(day)
    except Exception as e:  # noqa: BLE001 — 조회 실패면 만료도 하지 않는다
        logger.warning("파생 체결 조회 실패 %s: %s", account.id, e)
        out["failed_markets"] = ["KR"]
        return out
    multiplier = cfg.product_cfg().multiplier
    for order_no, lin in _lineages(fills).items():
        if lin["filled"] <= 0:
            continue
        with store.tx(conn):
            leg, newly_bound = _leg_for(conn, account.id, order_no, lin)
            if leg is None:
                oid, created = _external_order(conn, account.id, order_no, lin)
                if oid is None:
                    continue
                if created:
                    out["external"] += 1
                if _replace_fill(
                    conn, order_id=oid, account_id=account.id, filled=lin["filled"],
                    amount=lin["amount"], filled_at=lin["filled_at"],
                ):  # fmt: skip
                    out["inserted"] += 1
                _order_status(conn, oid, lin["filled"])
                continue
            if newly_bound:
                out["bound"] += 1
            oid = leg.order_id
            if oid is None:
                oid = repo.insert_order(
                    conn, decision_id=None, account_id=account.id, ticker=lin["code"], market="KR",
                    side=lin["side"], qty=leg.qty, order_type=leg.order_type, price=leg.price,
                    status="accepted", order_no=order_no, message="체결 동기화로 확정한 다리",
                    mode="recovered", asset="fo",
                    amount=Decimal(leg.qty) * (leg.price or Decimal(0)) * multiplier,
                )  # fmt: skip
                store.set_leg(conn, leg.id, order_id=oid)
            delta = 0
            if lin["filled"] > leg.filled_qty:
                # 증분 평균가 = Δ금액 ÷ Δ수량(누적 평균이 아니라 이번 증분만)
                prev_amt = _fill_amount(conn, oid)
                d_qty = lin["filled"] - leg.filled_qty
                d_amt = lin["amount"] - prev_amt
                inc_price = (d_amt / d_qty) if d_qty and d_amt > 0 else None
                delta = store.record_fill_progress(conn, leg.id, lin["filled"], lin["amount"])
                if inc_price is not None:
                    store.apply_fill_economics(
                        conn, leg.position_id, entry=is_entry(leg.intent), inc_price=inc_price,
                        delta=delta, multiplier=multiplier, cost_bp=_cost_bp(cfg, lin["code"]),
                    )  # fmt: skip
            if _replace_fill(
                conn, order_id=oid, account_id=account.id, filled=lin["filled"],
                amount=lin["amount"], filled_at=lin["filled_at"],
            ):  # fmt: skip
                out["updated" if delta == 0 else "inserted"] += 1
            _order_status(conn, oid, lin["filled"])
    if _session_over(day, now()):
        out["expired"] = expire_legs(conn, account.id, day)
    return out


def _fill_amount(conn: sqlite3.Connection, order_id: int) -> Decimal:
    """주문의 누적 체결 금액(Decimal 합, REAL 캐스트 없음)."""
    rows = conn.execute("SELECT qty, price FROM fills WHERE order_id = ?", (order_id,)).fetchall()
    return sum((Decimal(str(r["price"])) * int(r["qty"]) for r in rows), Decimal(0))


def _cost_bp(cfg: FoCfg, code: str) -> float:
    kind = "futures" if code.startswith(cfg.product_cfg().futures_prefix) else "option"
    return float(cfg.costs_bp.get(kind, 0.0))


def _external_order(
    conn: sqlite3.Connection, account_id: str, order_no: str, lin: dict[str, Any]
) -> tuple[int | None, bool]:
    """(orders id, 이번에 새로 만들었는가)."""
    row = conn.execute(
        "SELECT id FROM orders WHERE account_id = ? AND order_no = ? AND ticker = ? "
        "AND asset = 'fo' ORDER BY id DESC LIMIT 1",
        (account_id, order_no, lin["code"]),
    ).fetchone()
    if row:
        return int(row["id"]), False
    if lin["total"] <= 0:
        return None, False
    logger.info("파생 원장 밖 주문 %s %s %s → external", account_id, lin["code"], order_no)
    oid = repo.insert_order(
        conn, decision_id=None, account_id=account_id, ticker=lin["code"], market="KR",
        side=lin["side"], qty=lin["total"], order_type="LIMIT", price=None, status="accepted",
        order_no=order_no, message="원장 밖에서 낸 파생 주문(체결 동기화로 생성)", mode="external",
        created_at=lin["filled_at"].isoformat(), amount=lin["amount"], asset="fo",
    )  # fmt: skip
    return oid, True


def _session_over(day: date, now: datetime) -> bool:
    local = now.astimezone(KST)
    if local.date() > day:
        return True
    return local.date() == day and local.strftime("%H:%M") >= fo_close_hm(False)


def expire_legs(conn: sqlite3.Connection, account_id: str, day: date) -> int:
    """그날(KST) 낸 `accepted` 다리 중 잔량이 남은 것을 expired 로. 주문 행도 같이.
    "낸 시각" 은 접수·전송 시각(없으면 생성 시각)."""
    lo, hi = repo.day_bounds(day, "KR")
    n = 0
    with store.tx(conn):
        for leg in store.live_legs(conn, account_id=account_id):
            at = leg.accepted_at or leg.sent_at or leg.created_at
            if leg.status != "accepted" or at < lo or at >= hi:
                continue
            store.set_leg(conn, leg.id, status="expired")
            if leg.order_id is not None:
                conn.execute(
                    "UPDATE orders SET status = 'expired' WHERE id = ? AND status IN "
                    "('accepted', 'partial')",
                    (leg.order_id,),
                )
            n += 1
    if n:
        logger.info("파생 미체결 다리 %d건 만료 처리 %s %s", n, account_id, day)
    return n
