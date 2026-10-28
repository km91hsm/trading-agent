"""파생 체결 동기화(Phase 26 단계 2 Task 2.7, 스펙 §5 R15). `orchestrator/fills.sync_fills` 가
`account.asset == "fo"` 면 여기로 위임한다.

- 체결은 **원주문 계보**(`orgn_odno` 또는 `odno`) 단위로 누적값(`filled_qty`·`filled_amount`)을
  읽고 `fills` 행 1개를 **교체**한다(현물과 같은 방식, 차분 삽입 아님). KIS `filled_amount` 는
  **통화 금액**(포인트 × 수량 × 승수)이라 먼저 승수로 나눠 포인트 합으로 바꾸고(G2-1), 그 뒤의
  `fills.price`(파생 행은 **포인트 단가**)·증분 평균가(`Δ포인트 합 ÷ Δ수량`)·실현손실·비용은 전부
  포인트 기준이다. `orders.amount` 만 통화 금액 그대로.
- 진입 평균가의 가중 수량은 **경제값이 반영된 수량**(`qty_held` − 수량만 먼저 올라간 다리의 미반영
  체결)으로 센다(`_accounted_entry_qty`, G2-4) — 복구가 여러 다리를 먼저 확정해도 평균이 틀어지지
  않는다.
- 다리 매칭은 `order_no`(계보 원주문) → 같은 계좌·**같은 현지 날짜**(KIS 주문번호는 날마다
  재시작)의 `fo_order_legs.order_no`. 주문번호 없는 `sending|unknown` 다리는 같은 코드·방향·수량
  후보가 **정확히 1건**일 때만 묶는다. 어느 다리에도 붙지 않는 체결은 외부 주문(`orders(asset='fo',
  mode='external')`, 같은 날짜 범위)으로 적재한다.
- 경제값(평균가·비용·실현손실)은 **`fills` 행의 누적값 대비 증분**으로 1회 적는다 — 회수·복구가
  `record_fill_progress` 로 수량을 먼저 반영했어도 빠지지 않는다(수량 진행과 경제값 진행 분리).
- 계보 하나가 모순(누적 체결 > 다리 수량 등)이면 그 포지션만 unknown + 알림 1건 + `anomalies`
  집계하고 다른 계보는 계속 처리한다. 한 계보 = 한 트랜잭션(`store.tx`, 안에서 commit 하는 헬퍼
  없음).
- 만료는 **조회 성공 ∧ 파생 세션 종료 뒤**에만: 그날 체결 없는 `accepted` 다리·주문을 `expired`.
  만료로 빈 `entering` 포지션은 닫는다.
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
from trader.notify import messages

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


_LEG_AT = "COALESCE(accepted_at, sent_at, created_at)"


def _leg_for(
    conn: sqlite3.Connection, account_id: str, order_no: str, lin: dict[str, Any], day: date
) -> tuple[FoLegRow | None, bool]:
    """(다리, 이번에 새로 묶었는가). 같은 현지 날짜(접수·전송·생성 시각)의 다리만 본다."""
    lo, hi = repo.day_bounds(day, "KR")
    row = conn.execute(
        f"SELECT id FROM fo_order_legs WHERE account_id = ? AND order_no = ? "
        f"AND {_LEG_AT} >= ? AND {_LEG_AT} < ? ORDER BY id DESC LIMIT 1",
        (account_id, order_no, lo, hi),
    ).fetchone()
    if row:
        return store.get_leg(conn, int(row["id"])), False
    bound = {
        r["order_no"]
        for r in conn.execute(
            f"SELECT order_no FROM fo_order_legs WHERE order_no IS NOT NULL AND account_id = ? "
            f"AND {_LEG_AT} >= ? AND {_LEG_AT} < ?",
            (account_id, lo, hi),
        )
    }
    if order_no in bound:
        return None, False
    cands = []
    for leg in store.live_legs(conn, account_id=account_id):
        if leg.status not in ("sending", "unknown") or leg.order_no is not None:
            continue
        at = leg.accepted_at or leg.sent_at or leg.created_at
        if at < lo or at >= hi:
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
        filled_at=filled_at.isoformat(), commit=False,
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
        "external": 0, "bound": 0, "expired": 0, "anomalies": 0, "failed_markets": [],
    }  # fmt: skip
    try:
        fills = broker.get_fills(day)
    except Exception as e:  # noqa: BLE001 — 조회 실패면 만료도 하지 않는다
        logger.warning("파생 체결 조회 실패 %s: %s", account.id, e)
        out["failed_markets"] = ["KR"]
        return out
    for order_no, lin in _lineages(fills).items():
        if lin["filled"] <= 0:
            continue
        try:
            with store.tx(conn):
                _sync_lineage(conn, account, order_no, lin, day, cfg, out)
        except ValueError as e:
            # 계보 모순(누적 체결 > 다리 수량 등): 이 포지션만 unknown, 나머지 계보는 계속
            out["anomalies"] += 1
            logger.error("파생 체결 계보 모순 %s %s: %s", account.id, order_no, e)
            _flag_anomaly(conn, account.id, order_no, lin, day, notifier)
    if _session_over(day, now()):
        out["expired"] = expire_legs(conn, account.id, day)
    return out


def _sync_lineage(
    conn: sqlite3.Connection, account: AccountConfig, order_no: str, lin: dict[str, Any],
    day: date, cfg: FoCfg, out: dict[str, Any],
) -> None:  # fmt: skip
    """계보 하나를 반영한다(호출자 트랜잭션 안). 수량 진행은 `record_fill_progress`(이미 반영돼
    있으면 0), 경제값은 `fills` 행 누적 대비 증분으로 — 둘은 독립이라 어느 쪽이 먼저여도 1회."""
    multiplier = cfg.product_cfg().multiplier
    amount_pt = lin["amount"] / multiplier  # 통화 금액 → 포인트 합(G2-1)
    leg, newly_bound = _leg_for(conn, account.id, order_no, lin, day)
    if leg is None:
        oid, created = _external_order(conn, account.id, order_no, lin, day)
        if oid is None:
            return
        if created:
            out["external"] += 1
        if _replace_fill(
            conn, order_id=oid, account_id=account.id, filled=lin["filled"],
            amount=amount_pt, filled_at=lin["filled_at"],
        ):  # fmt: skip
            out["inserted"] += 1
        _order_status(conn, oid, lin["filled"])
        return
    if newly_bound:
        out["bound"] += 1
    oid = leg.order_id
    if oid is None:
        oid = repo.insert_order(
            conn, decision_id=None, account_id=account.id, ticker=lin["code"], market="KR",
            side=lin["side"], qty=leg.qty, order_type=leg.order_type, price=leg.price,
            status="accepted", order_no=order_no, message="체결 동기화로 확정한 다리",
            mode="recovered", asset="fo", commit=False,
            amount=Decimal(leg.qty) * (leg.price or Decimal(0)) * multiplier,
        )  # fmt: skip
        store.set_leg(conn, leg.id, order_id=oid)
    prev_qty, prev_amt = _fill_totals(conn, oid)
    d_qty = lin["filled"] - prev_qty
    accounted = _accounted_entry_qty(conn, leg.position_id) if is_entry(leg.intent) else None
    if lin["filled"] > leg.filled_qty:
        store.record_fill_progress(conn, leg.id, lin["filled"], lin["amount"])
    if d_qty > 0:
        # 증분 평균가 = Δ금액 ÷ Δ수량(누적 평균이 아니라 이번 증분만)
        d_amt = amount_pt - prev_amt
        if d_amt > 0:
            store.apply_fill_economics(
                conn, leg.position_id, entry=is_entry(leg.intent), inc_price=d_amt / d_qty,
                delta=d_qty, multiplier=multiplier, cost_bp=_cost_bp(cfg, lin["code"]),
                accounted_qty=accounted,
            )  # fmt: skip
    if _replace_fill(
        conn, order_id=oid, account_id=account.id, filled=lin["filled"],
        amount=amount_pt, filled_at=lin["filled_at"],
    ):  # fmt: skip
        out["inserted" if d_qty > 0 else "updated"] += 1
    _order_status(conn, oid, lin["filled"])


def _flag_anomaly(
    conn: sqlite3.Connection, account_id: str, order_no: str, lin: dict[str, Any], day: date,
    notifier: Any | None,
) -> None:  # fmt: skip
    leg, _ = _leg_for(conn, account_id, order_no, lin, day)
    if leg is None:
        return
    pos = store.get_position(conn, leg.position_id)
    if pos is None or pos.status in ("unknown", "closed", "settled"):
        return
    with store.tx(conn):
        store.transition(
            conn, pos.id, expected_version=pos.version, status="unknown",
            note=f"체결 모순: 주문 {order_no} 누적 {lin['filled']} > 다리 {leg.qty}",
        )  # fmt: skip
    if notifier is not None:
        try:
            notifier.send(
                messages.fo_fill_anomaly(
                    account=account_id, code=pos.code, name=None, leg_qty=leg.qty,
                    filled=lin["filled"], order_no=order_no,
                )  # fmt: skip
            )
        except Exception:  # noqa: BLE001
            logger.debug("파생 체결 모순 알림 실패", exc_info=True)


def _accounted_entry_qty(conn: sqlite3.Connection, position_id: int) -> int:
    """평균가에 이미 반영된 진입 수량 = `qty_held` − Σ(진입 다리 `filled_qty` − 그 주문의 `fills`
    누적 수량). 수량만 먼저 올라간(회수·복구) 체결은 아직 평균가 밖이다(G2-4)."""
    pos = store.get_position(conn, position_id)
    if pos is None:
        return 0
    rows = conn.execute(
        "SELECT l.filled_qty AS progressed, "
        "(SELECT COALESCE(SUM(f.qty), 0) FROM fills f WHERE f.order_id = l.order_id) AS acc "
        "FROM fo_order_legs l WHERE l.position_id = ? "
        "AND l.intent IN ('OPEN', 'RESIZE_UP')",
        (position_id,),
    ).fetchall()
    unaccounted = sum(max(int(r["progressed"]) - int(r["acc"]), 0) for r in rows)
    return max(pos.qty_held - unaccounted, 0)


def _fill_totals(conn: sqlite3.Connection, order_id: int) -> tuple[int, Decimal]:
    """주문의 `fills` 행 누적(수량, 포인트 합 = Σ price × qty, Decimal 합·REAL 캐스트 없음)."""
    rows = conn.execute("SELECT qty, price FROM fills WHERE order_id = ?", (order_id,)).fetchall()
    qty = sum(int(r["qty"]) for r in rows)
    amt = sum((Decimal(str(r["price"])) * int(r["qty"]) for r in rows), Decimal(0))
    return qty, amt


def _cost_bp(cfg: FoCfg, code: str) -> float:
    kind = "futures" if code.startswith(cfg.product_cfg().futures_prefix) else "option"
    return float(cfg.costs_bp.get(kind, 0.0))


def _external_order(
    conn: sqlite3.Connection, account_id: str, order_no: str, lin: dict[str, Any], day: date
) -> tuple[int | None, bool]:
    """(orders id, 이번에 새로 만들었는가). 같은 현지 날짜의 주문 행만 본다."""
    lo, hi = repo.day_bounds(day, "KR")
    row = conn.execute(
        "SELECT id FROM orders WHERE account_id = ? AND order_no = ? AND ticker = ? "
        "AND asset = 'fo' AND created_at >= ? AND created_at < ? ORDER BY id DESC LIMIT 1",
        (account_id, order_no, lin["code"], lo, hi),
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
        created_at=lin["filled_at"].isoformat(), amount=lin["amount"], asset="fo", commit=False,
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
            if is_entry(leg.intent):
                store.release_if_empty(conn, leg.position_id, f"진입 다리 {leg.id} 만료(체결 0)")
            n += 1
    if n:
        logger.info("파생 미체결 다리 %d건 만료 처리 %s %s", n, account_id, day)
    return n
