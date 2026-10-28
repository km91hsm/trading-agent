"""파생 레인 저장 계층(Phase 26 단계 2, 스펙 §5 R2·R3). **commit 하지 않는 헬퍼만** 두고 트랜잭션
경계는 호출자가 `with store.tx(conn):` 로 잡는다(`BEGIN IMMEDIATE` … commit/rollback). 현물
`repo.claim_exec_key` 는 쓰지 않는다 — FO 의 실행 키는 `fo_order_legs` 행 자체
(UNIQUE `(position_id, intent, round, attempt)`)다. `reserve` 가 한 잠금 안에서 계좌 총량
(종목 수·계약 수·수명 손실 예산·축소 다리 단일)을 **살아 있는 다리 포함**으로 다시 세고 포지션
전환(version+1) + 다리 `prepared` 삽입까지 한다. 실패면 아무것도 쓰지 않는다."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from trader.fo.types import (
    LIVE_LEG,
    OPEN_POSITION_STATUSES,
    FoLegRow,
    FoPositionRow,
    can_transition,
    is_entry,
    is_reduce,
)
from trader.ledger.db import now_iso

_POS_COLS = (
    "account_id",
    "code",
    "kind",
    "contract_side",
    "direction",
    "vehicle",
    "status",
    "qty_target",
    "qty_held",
    "leverage_used",
    "stop_pt",
    "stop_fut",
    "tp_pt",
    "approved_mult",
    "premium_stop",
    "entry_premium",
    "nav_at_open",
    "realized_loss",
    "fees",
    "active_reduce_leg_id",
    "horizon_until",
    "last_trade_date",
    "decision_date",
    "round",
    "version",
    "opened_at",
    "closed_at",
    "note",
)


def _s(v: Any) -> Any:
    """Decimal → TEXT. 그 외는 그대로(None 포함)."""
    return str(v) if isinstance(v, Decimal) else v


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """쓰기 트랜잭션. 안에서 예외가 나면 rollback 하고 다시 던진다. 앞선 DML 이 열어 둔 암묵
    트랜잭션은 먼저 commit 한다(`jobs.enqueue` 와 같은 관례)."""
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# --- 포지션 --------------------------------------------------------------------------


def get_position(conn: sqlite3.Connection, position_id: int) -> FoPositionRow | None:
    row = conn.execute("SELECT * FROM fo_positions WHERE id = ?", (position_id,)).fetchone()
    return FoPositionRow.from_row(row) if row else None


def open_positions(conn: sqlite3.Connection, account_id: str) -> list[FoPositionRow]:
    """자리를 차지하는 포지션(settled·closed 제외)."""
    marks = ",".join("?" * len(OPEN_POSITION_STATUSES))
    rows = conn.execute(
        f"SELECT * FROM fo_positions WHERE account_id = ? AND status IN ({marks}) ORDER BY id",
        (account_id, *OPEN_POSITION_STATUSES),
    ).fetchall()
    return [FoPositionRow.from_row(r) for r in rows]


def insert_position(conn: sqlite3.Connection, **fields: Any) -> int:
    unknown = set(fields) - set(_POS_COLS)
    if unknown:
        raise ValueError(f"fo_positions 에 없는 열: {sorted(unknown)}")
    for k in ("account_id", "code", "kind", "contract_side", "direction", "vehicle", "status"):
        if not fields.get(k):
            raise ValueError(f"포지션 필수 열 누락: {k}")
    cols = list(fields) + ["created_at"]
    vals = [_s(fields[c]) for c in fields] + [now_iso()]
    cur = conn.execute(
        f"INSERT INTO fo_positions({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        vals,
    )
    return int(cur.lastrowid)


def transition(
    conn: sqlite3.Connection, position_id: int, *, expected_version: int, **changes: Any
) -> bool:
    """CAS 갱신: version 이 기대값일 때만 바꾸고 version+1. 아니면 False(아무것도 안 바뀜).
    `status` 를 바꾸면 전이표(`types.TRANSITIONS`) 밖이면 ValueError — 어떤 호출자도 표를 우회하지
    못한다(단계 2 리뷰 C2-10)."""
    unknown = set(changes) - set(_POS_COLS) - {"version"}
    if unknown:
        raise ValueError(f"fo_positions 에 없는 열: {sorted(unknown)}")
    changes.pop("version", None)
    if "status" in changes:
        row = conn.execute(
            "SELECT status FROM fo_positions WHERE id = ?", (position_id,)
        ).fetchone()
        cur_status, new_status = (row["status"] if row else None), changes["status"]
        if cur_status not in (None, new_status) and not can_transition(cur_status, new_status):
            raise ValueError(f"포지션 {position_id} 전이 불가 {cur_status}→{new_status}")
    sets = ", ".join(f"{k} = ?" for k in changes) + (", " if changes else "") + "version = ?"
    vals = [_s(v) for v in changes.values()] + [expected_version + 1, position_id, expected_version]
    cur = conn.execute(
        f"UPDATE fo_positions SET {sets} WHERE id = ? AND version = ?",
        vals,
    )
    return cur.rowcount == 1


# --- 다리 ----------------------------------------------------------------------------


def live_legs(
    conn: sqlite3.Connection, *, position_id: int | None = None, account_id: str | None = None
) -> list[FoLegRow]:
    marks = ",".join("?" * len(LIVE_LEG))
    where = [f"status IN ({marks})"]
    params: list[Any] = list(LIVE_LEG)
    if position_id is not None:
        where.append("position_id = ?")
        params.append(position_id)
    if account_id is not None:
        where.append("account_id = ?")
        params.append(account_id)
    rows = conn.execute(
        f"SELECT * FROM fo_order_legs WHERE {' AND '.join(where)} ORDER BY id", params
    ).fetchall()
    return [FoLegRow.from_row(r) for r in rows]


def legs_of(conn: sqlite3.Connection, position_id: int) -> list[FoLegRow]:
    rows = conn.execute(
        "SELECT * FROM fo_order_legs WHERE position_id = ? ORDER BY id", (position_id,)
    ).fetchall()
    return [FoLegRow.from_row(r) for r in rows]


def get_leg(conn: sqlite3.Connection, leg_id: int) -> FoLegRow | None:
    row = conn.execute("SELECT * FROM fo_order_legs WHERE id = ?", (leg_id,)).fetchone()
    return FoLegRow.from_row(row) if row else None


def insert_leg(
    conn: sqlite3.Connection,
    *,
    position_id: int,
    account_id: str,
    intent: str,
    round: int,
    attempt: int,
    qty: int,
    price: Decimal | None,
    order_type: str,
    request_id: str | None = None,
) -> int:
    """`prepared` 다리 삽입. 실행 키 충돌은 `sqlite3.IntegrityError` 그대로. `request_id` 는 이
    다리를 만든 결정 봉투(원장 v27, 제출 재개 판정의 근거)."""
    if qty <= 0:
        raise ValueError("다리 수량은 1 이상")
    cur = conn.execute(
        "INSERT INTO fo_order_legs(position_id, account_id, intent, round, attempt, status, qty, "
        "price, order_type, created_at, request_id) "
        "VALUES (?, ?, ?, ?, ?, 'prepared', ?, ?, ?, ?, ?)",
        (position_id, account_id, intent, round, attempt, qty, _s(price), order_type, now_iso(),
         request_id),
    )
    return int(cur.lastrowid)


def set_leg(
    conn: sqlite3.Connection,
    leg_id: int,
    *,
    status: str | None = None,
    order_id: int | None = None,
    order_no: str | None = None,
    sent_at: str | None = None,
    accepted_at: str | None = None,
    filled_qty: int | None = None,
) -> None:
    changes = {
        k: v
        for k, v in (
            ("status", status),
            ("order_id", order_id),
            ("order_no", order_no),
            ("sent_at", sent_at),
            ("accepted_at", accepted_at),
            ("filled_qty", filled_qty),
        )
        if v is not None
    }
    if not changes:
        return
    sets = ", ".join(f"{k} = ?" for k in changes)
    conn.execute(f"UPDATE fo_order_legs SET {sets} WHERE id = ?", [*changes.values(), leg_id])


def claim_leg(conn: sqlite3.Connection, leg_id: int, *, sent_at: str | None = None) -> bool:
    """전송 권한 선점: `prepared` 일 때만 `sending`(sent_at, 호출자 시계) 으로 바꾼다.
    회수(`recall_entries`)가 먼저 cancelled 로 바꿨으면 False — 그 다리는 보내지 않는다(단계 2
    리뷰 S2-2). 전송·접수 시각은 체결 동기화의 날짜 범위 매칭 기준이라 실행 문맥의 시계를 받는다."""
    cur = conn.execute(
        "UPDATE fo_order_legs SET status = 'sending', sent_at = ? WHERE id = ? "
        "AND status = 'prepared'",
        (sent_at or now_iso(), leg_id),
    )
    return cur.rowcount == 1


def cancel_prepared(conn: sqlite3.Connection, leg_id: int) -> bool:
    """회수: **아직 `prepared` 일 때만** cancelled 로 바꾼다(조건부 UPDATE, rowcount 1 이 성공).
    전송기가 그사이 `claim_leg` 로 sending 을 선점했으면 False — 호출자는 그 다리를 pending 으로
    본다(GitHub 리뷰 G2-2)."""
    cur = conn.execute(
        "UPDATE fo_order_legs SET status = 'cancelled' WHERE id = ? AND status = 'prepared'",
        (leg_id,),
    )
    return cur.rowcount == 1


def release_if_empty(conn: sqlite3.Connection, position_id: int, note: str) -> bool:
    """보유 0·살아 있는 다리 0 인 `entering` 포지션을 닫아 자리를 비운다(진입 거부·취소·만료·회수
    뒤). 닫았으면 True. 호출자 트랜잭션 안에서 부른다."""
    pos = get_position(conn, position_id)
    if pos is None or pos.status != "entering" or pos.qty_held != 0:
        return False
    if live_legs(conn, position_id=position_id):
        return False
    return transition(
        conn, pos.id, expected_version=pos.version, status="closed", closed_at=now_iso(), note=note
    )


def next_attempt(conn: sqlite3.Connection, position_id: int, intent: str, round: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(attempt), 0) AS a FROM fo_order_legs "
        "WHERE position_id = ? AND intent = ? AND round = ?",
        (position_id, intent, round),
    ).fetchone()
    return int(row["a"]) + 1


# --- 총량·예약 -------------------------------------------------------------------------


def account_totals(conn: sqlite3.Connection, account_id: str) -> dict[str, Any]:
    """계좌의 자리 집계: 열린 포지션 수, 보유 계약 수, 살아 있는 진입 다리의 미체결 계약 수."""
    positions = open_positions(conn, account_id)
    legs = live_legs(conn, account_id=account_id)
    held = sum(p.qty_held for p in positions)
    live_entry = sum(leg.remaining for leg in legs if is_entry(leg.intent))
    return {
        "positions": len(positions),
        "contracts_held": held,
        "contracts_live": live_entry,
        "legs": legs,
        "position_rows": positions,
    }


@dataclass
class ReservePlan:
    """예약 요청. 새 포지션이면 `new_position`(insert_position 열), 기존이면 `position_id` +
    `expected_version`. `new_status` 는 전환할 상태(없으면 상태 유지). 수명 예산 검사(진입만):
    실현손실 + 비용 + `planned_loss_existing`(보유는 평균가·미체결은 다리 가격 기준, 사이징이 계산)
    + `planned_loss_per_contract` × 이번 다리 ≤ `budget_limit`. `planned_loss_existing` 이 없으면
    보유·미체결도 이번 단가로 센다(하위 호환)."""

    intent: str
    round: int
    qty: int
    price: Decimal | None
    order_type: str
    position_id: int | None = None
    expected_version: int | None = None
    new_position: dict[str, Any] | None = None
    new_status: str | None = None
    planned_loss_per_contract: Decimal = Decimal(0)
    request_id: str | None = None  # 다리를 만든 결정 봉투(제출 재개 판정)
    planned_loss_existing: Decimal | None = None
    budget_limit: Decimal | None = None
    position_changes: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReserveResult:
    ok: bool
    leg_id: int | None = None
    position_id: int | None = None
    version: int | None = None
    reasons: list[str] = field(default_factory=list)


def reserve(
    conn: sqlite3.Connection,
    account_id: str,
    plan: ReservePlan,
    *,
    max_positions: int,
    max_contracts: int,
) -> ReserveResult:
    """예약 트랜잭션 본체 — 호출자가 `with tx(conn):` 안에서 부른다(잠금 안에서 총량을 다시 센다).
    거부면 아무것도 쓰지 않고 `reasons` 만 돌려준다(호출자는 그래도 commit 해도 된다 — 쓴 것이
    없다)."""
    reasons: list[str] = []
    totals = account_totals(conn, account_id)
    pos: FoPositionRow | None = None
    if plan.position_id is not None:
        pos = get_position(conn, plan.position_id)
        if pos is None or pos.account_id != account_id:
            return ReserveResult(False, reasons=[f"포지션 {plan.position_id} 없음"])
        if plan.expected_version is not None and pos.version != plan.expected_version:
            return ReserveResult(
                False,
                reasons=[f"포지션 버전 불일치 {pos.version} ≠ 기대 {plan.expected_version}"],
            )
    elif plan.new_position is None:
        return ReserveResult(False, reasons=["포지션 id 또는 새 포지션 필요"])
    if plan.qty <= 0:
        return ReserveResult(False, reasons=["수량 0"])
    if (
        pos is not None
        and plan.new_status
        and plan.new_status != pos.status
        and not can_transition(pos.status, plan.new_status)
    ):
        return ReserveResult(False, reasons=[f"전이 불가 {pos.status}→{plan.new_status}"])

    if is_reduce(plan.intent):
        if pos is None:
            return ReserveResult(False, reasons=["축소 다리는 기존 포지션에만"])
        if pos.active_reduce_leg_id is not None:
            live = get_leg(conn, pos.active_reduce_leg_id)
            if live is not None and live.live:
                reasons.append(f"살아 있는 축소 다리 {live.id} 가 이미 있음")
        if plan.qty > pos.qty_held:
            reasons.append(f"축소 수량 {plan.qty} > 보유 {pos.qty_held}")
    elif is_entry(plan.intent):
        if pos is None and totals["positions"] + 1 > max_positions:
            reasons.append(f"종목 수 {totals['positions'] + 1} > 한도 {max_positions}")
        total_after = totals["contracts_held"] + totals["contracts_live"] + plan.qty
        if total_after > max_contracts:
            reasons.append(f"계약 수 {total_after} > 한도 {max_contracts}")
        if plan.budget_limit is not None:
            if plan.planned_loss_existing is not None:
                planned = plan.planned_loss_existing + plan.planned_loss_per_contract * plan.qty
            else:
                held = pos.qty_held if pos else 0
                live = sum(
                    leg.remaining
                    for leg in totals["legs"]
                    if pos is not None and leg.position_id == pos.id and is_entry(leg.intent)
                )
                planned = plan.planned_loss_per_contract * (held + live + plan.qty)
            used = (pos.realized_loss + pos.fees) if pos else Decimal(0)
            if used + planned > plan.budget_limit:
                reasons.append(
                    f"수명 손실 예산 초과: 사용 {used} + 계획 {planned} > 한도 {plan.budget_limit}"
                )
    else:
        return ReserveResult(False, reasons=[f"알 수 없는 의도 {plan.intent}"])
    if reasons:
        return ReserveResult(False, reasons=reasons)

    if pos is None:
        assert plan.new_position is not None
        fields = dict(plan.new_position)
        fields.setdefault("status", plan.new_status or "entering")
        fields.setdefault("round", plan.round)
        position_id = insert_position(conn, **fields)
        version = 0
    else:
        position_id = pos.id
        changes = dict(plan.position_changes)
        if plan.new_status:
            changes["status"] = plan.new_status
        if not transition(conn, pos.id, expected_version=pos.version, **changes):
            return ReserveResult(False, reasons=["포지션 전환 실패(동시 변경)"])
        version = pos.version + 1
    attempt = next_attempt(conn, position_id, plan.intent, plan.round)
    leg_id = insert_leg(
        conn,
        position_id=position_id,
        account_id=account_id,
        intent=plan.intent,
        round=plan.round,
        attempt=attempt,
        qty=plan.qty,
        price=plan.price,
        order_type=plan.order_type,
        request_id=plan.request_id,
    )
    if is_reduce(plan.intent):
        if not transition(conn, position_id, expected_version=version, active_reduce_leg_id=leg_id):
            return ReserveResult(False, reasons=["축소 다리 연결 실패(동시 변경)"])
        version += 1
    return ReserveResult(True, leg_id=leg_id, position_id=position_id, version=version)


# --- 결정·스냅샷·체결 진행 --------------------------------------------------------------

_DEC_COLS = (
    "date",
    "account_id",
    "position_id",
    "round",
    "request_id",
    "intent",
    "direction",
    "vehicle",
    "target_leverage",
    "leverage_used",
    "approved_mult",
    "confidence",
    "index_at_decision",
    "quote_asof",
    "data",
)


def record_decision(conn: sqlite3.Connection, **fields: Any) -> int:
    """`fo_decisions` 1행(UNIQUE date·account·position·round 충돌은 IntegrityError 그대로)."""
    unknown = set(fields) - set(_DEC_COLS)
    if unknown:
        raise ValueError(f"fo_decisions 에 없는 열: {sorted(unknown)}")
    for k in ("date", "request_id", "intent", "direction", "vehicle", "data"):
        if fields.get(k) is None:
            raise ValueError(f"결정 필수 열 누락: {k}")
    cols = list(fields) + ["created_at"]
    vals = [_s(fields[c]) for c in fields] + [now_iso()]
    cur = conn.execute(
        f"INSERT INTO fo_decisions({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        vals,
    )
    return int(cur.lastrowid)


def write_nav_snapshot(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    date: str,
    nav: Decimal | None,
    cash: Decimal | None,
    margin_total: Decimal | None,
    add_margin: Decimal | None,
) -> None:
    conn.execute(
        "INSERT INTO fo_nav_snapshot(account_id, date, nav, cash, margin_total, add_margin, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(account_id, date) DO UPDATE SET "
        "nav=excluded.nav, cash=excluded.cash, margin_total=excluded.margin_total, "
        "add_margin=excluded.add_margin, created_at=excluded.created_at",
        (account_id, date, _s(nav), _s(cash), _s(margin_total), _s(add_margin), now_iso()),
    )


def write_positions_snapshot(
    conn: sqlite3.Connection, *, account_id: str, date: str, rows: list[dict[str, Any]]
) -> None:
    """그날 보유 스냅샷을 통째로 바꾼다(행이 없으면 그날 보유 없음)."""
    conn.execute(
        "DELETE FROM fo_positions_snapshot WHERE account_id = ? AND date = ?", (account_id, date)
    )
    t = now_iso()
    for r in rows:
        conn.execute(
            "INSERT INTO fo_positions_snapshot(account_id, date, code, kind, contract_side, qty, "
            "avg, current, delta, delta_notional, eval, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                account_id,
                date,
                r["code"],
                r["kind"],
                r["contract_side"],
                int(r["qty"]),
                _s(r.get("avg")),
                _s(r.get("current")),
                r.get("delta"),
                _s(r.get("delta_notional")),
                _s(r.get("eval")),
                t,
            ),
        )


def record_fill_progress(
    conn: sqlite3.Connection, leg_id: int, filled_qty: int, filled_amount: Decimal | None = None
) -> int:
    """다리 누적 체결 수량을 반영하고 포지션 `qty_held` 를 증분으로 갱신한다(진입 +, 축소 −).
    다 채워지면 다리 `filled`. 실현손익·비용·평균가는 체결 동기화(fo/fills)가 `fills` 행 기준으로
    따로 적으므로(수량 진행과 경제값 진행은 분리, 단계 2 리뷰 S2-3) 어느 경로가 먼저 수량을
    반영해도 경제값은 빠지지 않는다. 반환값은 반영한 증분."""
    leg = get_leg(conn, leg_id)
    if leg is None:
        raise ValueError(f"다리 {leg_id} 없음")
    delta = filled_qty - leg.filled_qty
    if delta < 0:
        raise ValueError(f"누적 체결이 줄 수 없음: 다리 {leg_id} {leg.filled_qty} → {filled_qty}")
    if filled_qty > leg.qty:
        raise ValueError(f"체결 {filled_qty} > 주문 {leg.qty} (다리 {leg_id})")
    if delta == 0:
        return 0
    status = "filled" if filled_qty >= leg.qty else None
    set_leg(conn, leg_id, filled_qty=filled_qty, status=status)
    sign = 1 if is_entry(leg.intent) else -1
    conn.execute(
        "UPDATE fo_positions SET qty_held = qty_held + ?, version = version + 1 WHERE id = ?",
        (sign * delta, leg.position_id),
    )
    return delta


def apply_fill_economics(
    conn: sqlite3.Connection,
    position_id: int,
    *,
    entry: bool,
    inc_price: Decimal,
    delta: int,
    multiplier: int,
    cost_bp: float,
    accounted_qty: int | None = None,
) -> None:
    """체결 증분을 포지션 경제값에 반영한다(체결 동기화 전용). 진입은 평균가(`entry_premium`)를
    수량 가중으로 갱신하고, 축소는 평균가 대비 실현손실을 `realized_loss` 에 더한다(이익은 예산을
    재충전하지 않으므로 0 으로 자른다). 비용은 체결 명목 × bp/2(왕복 bp 의 한쪽). `inc_price` 는
    **포인트**(통화 금액이 아님). `accounted_qty` 는 지금 평균가에 반영된 수량 — 복구가 수량만 먼저
    올린 다른 다리의 체결이 가중에 섞이지 않게 호출자(fo/fills)가 센다(G2-4); 없으면 `qty_held −
    delta`."""
    if delta <= 0:
        return
    pos = get_position(conn, position_id)
    if pos is None:
        return
    fee = inc_price * delta * multiplier * Decimal(str(cost_bp)) / Decimal(20_000)
    changes: dict[str, Any] = {"fees": pos.fees + fee}
    if entry:
        if accounted_qty is not None:
            before = max(accounted_qty, 0)
        else:
            before = max(pos.qty_held - delta, 0)  # record_fill_progress 가 먼저 더했다
        if pos.entry_premium is None or before == 0:
            changes["entry_premium"] = inc_price
        else:
            changes["entry_premium"] = (pos.entry_premium * before + inc_price * delta) / (
                before + delta
            )
    elif pos.entry_premium is not None:
        sign = 1 if pos.contract_side == "LONG" else -1
        pnl = (inc_price - pos.entry_premium) * sign * delta * multiplier
        if pnl < 0:
            changes["realized_loss"] = pos.realized_loss - pnl
    cur = get_position(conn, position_id)
    assert cur is not None
    transition(conn, position_id, expected_version=cur.version, **changes)
