"""파생 포지션 상태기계·재시작 복구(Phase 26 단계 2, 스펙 §5). 전이표는 여기 하나, 저장은
`fo/store`. 복구는 원장(포지션·다리) 과 브로커(잔고·체결·주문 상태)를 대조해 승계·`unknown`·
`awaiting_settlement` 를 가르고, 잔고에만 있는 확정 보유는 `unknown` 행으로 적어 사람이
`adopt` 할 때까지 어떤 자동 주문의 대상도 되지 않게 한다. 미확정 보유(`UnverifiedHolding`)는
행도 만들지 않는다 — 신규 금지 + 알림만."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from trader.broker.models import FoBalance, FoFill, FoOrderState
from trader.fo import store
from trader.fo.types import FoLegRow, FoPositionRow, is_entry, is_reduce
from trader.notify import messages

log = logging.getLogger("trader.fo")

TRANSITIONS: dict[str, set[str]] = {
    "entering": {"held", "closed", "unknown", "resizing", "closing"},
    "held": {"resizing", "closing", "unknown", "awaiting_settlement"},
    "resizing": {"held", "closing", "unknown"},
    "closing": {"closed", "exit_failed", "unknown", "awaiting_settlement", "held"},
    "exit_failed": {"closing", "closed", "unknown"},
    "awaiting_settlement": {"settled", "unknown"},
    "unknown": {"held", "closed"},  # 사람(adopt·close)만
    "settled": set(),
    "closed": set(),
}


def can_transition(a: str, b: str) -> bool:
    return b in TRANSITIONS.get(a, set())


@dataclass
class RecoveryReport:
    adopted: list[int] = field(default_factory=list)  # 승계(일치)한 포지션 id
    unknown: list[int] = field(default_factory=list)  # unknown 으로 바뀐·만든 포지션 id
    unverified: list[str] = field(default_factory=list)  # 미확정 잔고 행 사유
    settling: list[int] = field(default_factory=list)  # awaiting_settlement 로 간 포지션
    legs_fixed: int = 0
    alerts: list[str] = field(default_factory=list)


def _mark_unknown(
    conn: sqlite3.Connection, pos: FoPositionRow, note: str, rep: RecoveryReport
) -> None:
    if pos.status == "unknown":
        return
    store.transition(conn, pos.id, expected_version=pos.version, status="unknown", note=note)
    rep.unknown.append(pos.id)
    log.warning("파생 포지션 %s unknown: %s", pos.id, note)


def _confirm_leg(
    conn: sqlite3.Connection,
    leg: FoLegRow,
    order_state_of: Callable[[str, date], FoOrderState],
    fills: list[FoFill],
    today: date,
) -> bool:
    """`sending|unknown` 다리를 체결 조회로 확정한다. 확정하면 True. 주문번호가 없는 다리는 같은
    코드·방향·수량의 체결 후보가 **정확히 1건**일 때만 묶는다."""
    pos = store.get_position(conn, leg.position_id)
    if pos is None:
        return False
    order_no = leg.order_no
    if order_no is None:
        side = "BUY" if (is_entry(leg.intent) == (pos.contract_side == "LONG")) else "SELL"
        bound = {
            r["order_no"]
            for r in conn.execute(
                "SELECT order_no FROM fo_order_legs WHERE order_no IS NOT NULL AND account_id = ?",
                (leg.account_id,),
            )
        }
        cands = [
            f
            for f in fills
            if f.code == pos.code
            and f.side == side
            and f.filled_qty + f.remaining_qty + f.rejected_qty == leg.qty
            and f.order_no not in bound
        ]
        if len(cands) != 1:
            return False
        order_no = cands[0].order_no
    try:
        st = order_state_of(order_no, today)
    except Exception as e:  # noqa: BLE001 — 조회 실패는 미확정 유지
        log.warning("파생 다리 %s 주문 상태 조회 실패: %s", leg.id, e)
        return False
    if st.order_qty == 0 and not st.terminal:
        return False  # 원주문 행 없음 — unknown 유지
    if st.filled_qty > leg.filled_qty:
        store.record_fill_progress(conn, leg.id, st.filled_qty)
    if st.terminal:
        status = "filled" if st.state == "filled" else st.state
    else:
        status = "accepted"
    store.set_leg(conn, leg.id, status=status, order_no=order_no)
    return True


def recover(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    balance: FoBalance,
    order_state_of: Callable[[str, date], FoOrderState],
    fills_of: Callable[[date], list[FoFill]],
    today: date,
    now: datetime,
) -> RecoveryReport:
    """재시작·첫 틱 복구. 호출자가 commit 한다(헬퍼는 commit 하지 않음)."""
    rep = RecoveryReport()
    try:
        fills = fills_of(today)
    except Exception as e:  # noqa: BLE001
        log.warning("파생 체결 조회 실패(복구는 주문 상태만으로): %s", e)
        fills = []
    # ① 미확정 다리
    for leg in store.live_legs(conn, account_id=account_id):
        if leg.status not in ("sending", "unknown"):
            continue
        if _confirm_leg(conn, leg, order_state_of, fills, today):
            rep.legs_fixed += 1
        else:
            pos = store.get_position(conn, leg.position_id)
            if pos is not None:
                _mark_unknown(conn, pos, f"다리 {leg.id} 확정 불가", rep)
    # ② 원장 포지션 vs 잔고
    broker = {p.code: p for p in balance.positions}
    seen: set[str] = set()
    for pos in store.open_positions(conn, account_id):
        if pos.status in ("settled", "closed"):
            continue
        row = broker.get(pos.code)
        legs = store.live_legs(conn, position_id=pos.id)
        if row is not None:
            seen.add(pos.code)
            if pos.status == "unknown":
                continue
            if row.contract_side != pos.contract_side:
                _mark_unknown(
                    conn,
                    pos,
                    f"방향 불일치 원장 {pos.contract_side} ≠ 브로커 {row.contract_side}",
                    rep,
                )
                continue
            if row.qty == pos.qty_held:
                rep.adopted.append(pos.id)
                continue
            # 수량 불일치 — 살아 있는 다리(부분체결 미반영)가 설명하면 그대로 둔다
            explained = (row.qty > pos.qty_held and any(is_entry(x.intent) for x in legs)) or (
                row.qty < pos.qty_held and any(is_reduce(x.intent) for x in legs)
            )
            if explained:
                rep.adopted.append(pos.id)
            else:
                _mark_unknown(conn, pos, f"수량 불일치 원장 {pos.qty_held} ≠ 브로커 {row.qty}", rep)
            continue
        # 브로커에 없음
        if pos.status == "unknown":
            continue
        if pos.qty_held == 0 and not any(is_entry(x.intent) and x.filled_qty > 0 for x in legs):
            rep.adopted.append(pos.id)  # 아직 아무것도 체결되지 않은 진입 — 그대로
            continue
        expired = bool(pos.last_trade_date) and date.fromisoformat(pos.last_trade_date) < today
        if expired and can_transition(pos.status, "awaiting_settlement"):
            store.transition(
                conn, pos.id, expected_version=pos.version, status="awaiting_settlement"
            )
            rep.settling.append(pos.id)
        elif any(is_reduce(x.intent) for x in legs):
            rep.adopted.append(pos.id)  # 청산 중 — 체결 동기화가 닫는다
        else:
            _mark_unknown(conn, pos, "브로커 잔고에 없음(체결 설명 없음)", rep)
    # ③ 잔고에만 있는 확정 보유 → unknown 행
    for code, row in broker.items():
        if code in seen:
            continue
        pid = store.insert_position(
            conn,
            account_id=account_id,
            code=code,
            kind=row.kind,
            contract_side=row.contract_side,
            direction=row.contract_side
            if row.kind != "PUT"
            else ("SHORT" if row.contract_side == "LONG" else "LONG"),
            vehicle="FUTURES"
            if row.kind == "FUTURES"
            else ("OPTION_BUY" if row.contract_side == "LONG" else "OPTION_SELL"),
            status="unknown",
            qty_held=row.qty,
            entry_premium=row.avg_price if row.kind != "FUTURES" else None,
            note="복구: 원장에 없는 보유",
            opened_at=now.isoformat(),
        )
        rep.unknown.append(pid)
        rep.alerts.append(
            messages.fo_unknown_holding(
                account=account_id, code=code, side=row.contract_side, qty=row.qty
            )
        )
    # ④ 미확정 행
    if balance.unverified:
        rep.unverified = [f"{u.code}: {u.reason}" for u in balance.unverified]
        rep.alerts.append(messages.fo_field_unverified(account=account_id, items=rep.unverified))
    return rep


def adopt(
    conn: sqlite3.Connection,
    *,
    position_id: int,
    side: str,
    kind: str,
    qty: int,
    stop_pt: Decimal,
    balance: FoBalance,
) -> bool:
    """사람의 승계: 브로커 행(코드·방향·종류·수량)과 **대조 일치할 때만** `held` 로 올린다."""
    pos = store.get_position(conn, position_id)
    if pos is None or pos.status != "unknown":
        return False
    row = next((p for p in balance.positions if p.code == pos.code), None)
    if row is None or (row.contract_side, row.kind, row.qty) != (side, kind, qty):
        return False
    return store.transition(
        conn,
        pos.id,
        expected_version=pos.version,
        status="held",
        contract_side=side,
        kind=kind,
        qty_held=qty,
        qty_target=qty,
        stop_pt=stop_pt,
        note="adopt",
    )


def has_blocking_state(
    conn: sqlite3.Connection, account_id: str, *, unverified: list[str] | None = None
) -> str | None:
    """신규 진입을 막는 상태의 사유. unknown 포지션·미확정 다리·미확정 잔고 행."""
    unknown = [p.id for p in store.open_positions(conn, account_id) if p.status == "unknown"]
    if unknown:
        return f"unknown 포지션 {len(unknown)}건"
    pending = [
        x.id
        for x in store.live_legs(conn, account_id=account_id)
        if x.status in ("sending", "unknown")
    ]
    if pending:
        return f"미확정 다리 {len(pending)}건"
    if unverified:
        return f"미확정 잔고 행 {len(unverified)}건"
    return None
