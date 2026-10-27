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
from typing import Literal

from trader.broker.models import FoBalance, FoFill, FoOrderState
from trader.fo import store
from trader.fo.types import (
    TRANSITIONS,
    FoLegRow,
    FoPositionRow,
    can_transition,
    is_entry,
    is_reduce,
)
from trader.notify import messages

log = logging.getLogger("trader.fo")

__all__ = ["TRANSITIONS", "can_transition", "RecoveryReport", "recover", "adopt"]

ConfirmResult = Literal["confirmed", "unconfirmed", "error"]


@dataclass
class RecoveryReport:
    adopted: list[int] = field(default_factory=list)  # 승계(일치)한 포지션 id
    unknown: list[int] = field(default_factory=list)  # unknown 으로 바뀐·만든 포지션 id
    unverified: list[str] = field(default_factory=list)  # 미확정 잔고 행 사유
    settling: list[int] = field(default_factory=list)  # awaiting_settlement 로 간 포지션
    deferred: list[int] = field(default_factory=list)  # 조회 실패로 이번엔 못 확정한 다리 id
    legs_fixed: int = 0
    alerts: list[str] = field(default_factory=list)


def _mark_unknown(
    conn: sqlite3.Connection, pos: FoPositionRow, note: str, rep: RecoveryReport
) -> None:
    if pos.status == "unknown":
        return
    with store.tx(conn):
        store.transition(conn, pos.id, expected_version=pos.version, status="unknown", note=note)
    rep.unknown.append(pos.id)
    log.warning("파생 포지션 %s unknown: %s", pos.id, note)


def _confirm_leg(
    conn: sqlite3.Connection,
    leg: FoLegRow,
    order_state_of: Callable[[str, date], FoOrderState],
    fills: list[FoFill] | None,
    today: date,
) -> ConfirmResult:
    """`sending|unknown` 다리를 체결 조회로 확정한다. `confirmed` 면 다리 상태·체결 진행을 썼다.
    `unconfirmed` 는 조회는 됐지만 묶을 수 없음(원주문 행 없음·후보 0 또는 2건 이상 — 호출자가
    포지션을 unknown 으로), `error` 는 브로커 조회 실패(이번 틱은 보류, 격상 없음 — 단계 2 리뷰
    C2-5). 브로커 호출은 쓰기 잠금 밖에서 하고 반영만 짧은 트랜잭션으로 한다(S2-12). 주문번호가
    없는 다리는 같은 코드·방향·수량의 체결 후보가 **정확히 1건**일 때만 묶는다."""
    pos = store.get_position(conn, leg.position_id)
    if pos is None:
        return "unconfirmed"
    order_no = leg.order_no
    if order_no is None:
        if fills is None:
            return "error"  # 체결 조회 실패 — 후보를 셀 수 없다
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
            return "unconfirmed"
        order_no = cands[0].order_no
    try:
        st = order_state_of(order_no, today)
    except Exception as e:  # noqa: BLE001 — 조회 실패는 보류(미확정 유지, 격상 없음)
        log.warning("파생 다리 %s 주문 상태 조회 실패(보류): %s", leg.id, e)
        return "error"
    if st.order_qty == 0 and not st.terminal:
        return "unconfirmed"  # 원주문 행 없음 — unknown 유지
    if st.terminal:
        status = "filled" if st.state == "filled" else st.state
    else:
        status = "accepted"
    with store.tx(conn):
        if st.filled_qty > leg.filled_qty:
            store.record_fill_progress(conn, leg.id, st.filled_qty)
        store.set_leg(
            conn, leg.id, status=status, order_no=order_no,
            accepted_at=leg.accepted_at or (None if st.terminal else store.now_iso()),
        )  # fmt: skip
        if st.terminal and is_entry(leg.intent):
            store.release_if_empty(conn, leg.position_id, f"진입 다리 {leg.id} {status}(체결 0)")
    return "confirmed"


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
    """재시작·첫 틱 복구. 다리 확정·unknown 표시는 짧은 트랜잭션으로 그때그때 commit 하고(브로커
    조회 중 쓰기 잠금을 쥐지 않게), 나머지 쓰기는 호출자가 commit 한다."""
    rep = RecoveryReport()
    fills: list[FoFill] | None
    try:
        fills = fills_of(today)
    except Exception as e:  # noqa: BLE001
        log.warning("파생 체결 조회 실패(주문번호 없는 다리는 보류): %s", e)
        fills = None
    # ① 미확정 다리
    for leg in store.live_legs(conn, account_id=account_id):
        if leg.status not in ("sending", "unknown"):
            continue
        res = _confirm_leg(conn, leg, order_state_of, fills, today)
        if res == "confirmed":
            rep.legs_fixed += 1
        elif res == "error":
            rep.deferred.append(leg.id)
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
            # 아직 아무것도 체결되지 않은 진입 — 살아 있는 다리가 있으면 그대로, 없으면 닫는다
            if not legs and store.release_if_empty(conn, pos.id, "복구: 체결 없는 빈 진입"):
                continue
            rep.adopted.append(pos.id)
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
