"""Phase 26 단계 2 Task 2.2: 저장 계층 — commit 없는 헬퍼, 예약 트랜잭션(총량 재검사), CAS 전환."""

from __future__ import annotations

import sqlite3
import threading
from decimal import Decimal

import pytest

from trader.fo import store
from trader.fo.store import ReservePlan
from trader.ledger import db

ACCT = "paper-fo"


def _new_pos(**over):
    base = {
        "account_id": ACCT,
        "code": "105W11",
        "kind": "FUTURES",
        "contract_side": "LONG",
        "direction": "LONG",
        "vehicle": "FUTURES",
        "nav_at_open": Decimal("10000000"),
        "stop_pt": Decimal("345"),
    }
    base.update(over)
    return base


def _open(conn, qty=2, **over) -> store.ReserveResult:
    with store.tx(conn):
        r = store.reserve(
            conn,
            ACCT,
            ReservePlan(
                intent="OPEN",
                round=0,
                qty=qty,
                price=Decimal("350.5"),
                order_type="LIMIT",
                new_position=_new_pos(**over),
                planned_loss_per_contract=Decimal("100000"),
                budget_limit=Decimal("500000"),
            ),
            max_positions=1,
            max_contracts=4,
        )
    return r


def test_helpers_do_not_commit(conn):
    """헬퍼는 commit 하지 않는다 — tx 밖에서 부르고 rollback 하면 사라진다."""
    conn.execute("BEGIN")
    pid = store.insert_position(conn, **_new_pos(status="entering"))
    store.insert_leg(
        conn,
        position_id=pid,
        account_id=ACCT,
        intent="OPEN",
        round=0,
        attempt=1,
        qty=1,
        price=None,
        order_type="MARKET",
    )
    conn.execute("ROLLBACK")
    assert store.get_position(conn, pid) is None
    assert store.live_legs(conn, account_id=ACCT) == []


def test_tx_rolls_back_on_error(conn):
    with pytest.raises(RuntimeError), store.tx(conn):
        store.insert_position(conn, **_new_pos(status="entering"))
        raise RuntimeError("x")
    assert store.open_positions(conn, ACCT) == []
    assert conn.in_transaction is False


def test_reserve_new_position_inserts_prepared_leg(conn):
    r = _open(conn)
    assert r.ok and r.leg_id and r.position_id and r.version == 0
    pos = store.get_position(conn, r.position_id)
    assert pos.status == "entering" and pos.qty_held == 0 and pos.realized_loss == Decimal(0)
    leg = store.get_leg(conn, r.leg_id)
    assert (leg.status, leg.intent, leg.attempt, leg.qty, leg.price) == (
        "prepared",
        "OPEN",
        1,
        2,
        Decimal("350.5"),
    )


def test_reserve_two_new_positions_share_capacity(project):
    """두 연결이 max_positions=1 의 마지막 자리를 함께 잡지 못한다(BEGIN IMMEDIATE 직렬화)."""
    c0 = db.connect()
    db.init_schema(c0)
    c0.close()
    results: dict[str, store.ReserveResult] = {}
    gate = threading.Barrier(2)

    def worker(name: str):
        c = db.connect()
        try:
            gate.wait()
            results[name] = _open(c)
        finally:
            c.close()

    ts = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    oks = [r.ok for r in results.values()]
    assert sorted(oks) == [False, True]
    loser = next(r for r in results.values() if not r.ok)
    assert any("종목 수" in s for s in loser.reasons)
    c = db.connect()
    assert len(store.open_positions(c, ACCT)) == 1
    c.close()


def test_reserve_counts_live_legs_in_contracts(conn):
    """계약 수 한도는 보유 + 살아 있는 진입 다리의 미체결 + 이번 다리."""
    r = _open(conn, qty=3)
    assert r.ok
    pos = store.get_position(conn, r.position_id)
    with store.tx(conn):
        r2 = store.reserve(
            conn,
            ACCT,
            ReservePlan(
                intent="RESIZE_UP",
                round=0,
                qty=2,
                price=None,
                order_type="MARKET",
                position_id=pos.id,
                expected_version=pos.version,
                new_status="resizing",
            ),
            max_positions=1,
            max_contracts=4,
        )
    assert not r2.ok and any("계약 수 5" in s for s in r2.reasons)
    assert store.get_position(conn, pos.id).version == pos.version  # 아무것도 안 바뀜
    # 다리가 1계약 체결되고 나머지 2는 살아 있어도 합은 그대로 3
    store.record_fill_progress(conn, r.leg_id, 1)
    with store.tx(conn):
        r3 = store.reserve(
            conn,
            ACCT,
            ReservePlan(
                intent="RESIZE_UP",
                round=0,
                qty=1,
                price=None,
                order_type="MARKET",
                position_id=pos.id,
                expected_version=pos.version + 1,
                new_status="resizing",
            ),
            max_positions=1,
            max_contracts=4,
        )
    assert r3.ok and store.get_position(conn, pos.id).status == "resizing"


def test_reserve_lifetime_budget_counts_realized_and_live(conn):
    """수명 예산: 실현손실 + 비용 + 계획 손실(보유 + 살아 있는 진입 + 이번) ≤ 한도. 재충전 없음."""
    r = _open(conn, qty=1)
    pos = store.get_position(conn, r.position_id)
    store.record_fill_progress(conn, r.leg_id, 1)
    conn.execute(
        "UPDATE fo_positions SET realized_loss = '150000', status = 'held' WHERE id = ?", (pos.id,)
    )
    pos = store.get_position(conn, pos.id)
    with store.tx(conn):
        r2 = store.reserve(
            conn,
            ACCT,
            ReservePlan(
                intent="RESIZE_UP",
                round=1,
                qty=1,
                price=None,
                order_type="MARKET",
                position_id=pos.id,
                expected_version=pos.version,
                planned_loss_per_contract=Decimal("100000"),
                budget_limit=Decimal("200000"),
            ),
            max_positions=1,
            max_contracts=4,
        )
    assert not r2.ok and any("수명 손실 예산" in s for s in r2.reasons)


def test_reserve_rejects_second_reduce_leg(conn):
    r = _open(conn, qty=2)
    pos = store.get_position(conn, r.position_id)
    store.record_fill_progress(conn, r.leg_id, 2)
    pos = store.get_position(conn, pos.id)
    plan = ReservePlan(
        intent="CLOSE",
        round=0,
        qty=2,
        price=None,
        order_type="MARKET",
        position_id=pos.id,
        expected_version=pos.version,
        new_status="closing",
    )
    with store.tx(conn):
        r2 = store.reserve(conn, ACCT, plan, max_positions=1, max_contracts=4)
    assert r2.ok
    pos = store.get_position(conn, pos.id)
    assert pos.active_reduce_leg_id == r2.leg_id and pos.status == "closing"
    plan2 = ReservePlan(
        intent="STOP",
        round=0,
        qty=1,
        price=None,
        order_type="MARKET",
        position_id=pos.id,
        expected_version=pos.version,
    )
    with store.tx(conn):
        r3 = store.reserve(conn, ACCT, plan2, max_positions=1, max_contracts=4)
    assert not r3.ok and any("축소 다리" in s for s in r3.reasons)
    # 축소 수량 > 보유도 거부
    store.set_leg(conn, r2.leg_id, status="cancelled")
    plan3 = ReservePlan(
        intent="CLOSE",
        round=0,
        qty=3,
        price=None,
        order_type="MARKET",
        position_id=pos.id,
        expected_version=pos.version,
    )
    with store.tx(conn):
        r4 = store.reserve(conn, ACCT, plan3, max_positions=1, max_contracts=4)
    assert not r4.ok and any("보유" in s for s in r4.reasons)


def test_reduce_ignores_position_and_contract_caps(conn):
    """축소 다리는 종목 수·계약 수·예산과 무관하다."""
    r = _open(conn, qty=4)
    store.record_fill_progress(conn, r.leg_id, 4)
    pos = store.get_position(conn, r.position_id)
    with store.tx(conn):
        r2 = store.reserve(
            conn,
            ACCT,
            ReservePlan(
                intent="RISK_REDUCE",
                round=0,
                qty=1,
                price=None,
                order_type="MARKET",
                position_id=pos.id,
                expected_version=pos.version,
                budget_limit=Decimal("0"),
                planned_loss_per_contract=Decimal("1"),
            ),
            max_positions=0,
            max_contracts=0,
        )
    assert r2.ok


def test_transition_cas_version_mismatch_false(conn):
    r = _open(conn)
    pos = store.get_position(conn, r.position_id)
    assert store.transition(conn, pos.id, expected_version=pos.version + 5, status="held") is False
    assert store.get_position(conn, pos.id).status == "entering"
    assert store.transition(conn, pos.id, expected_version=pos.version, status="held") is True
    after = store.get_position(conn, pos.id)
    assert after.status == "held" and after.version == pos.version + 1
    with pytest.raises(ValueError):
        store.transition(conn, pos.id, expected_version=after.version, bogus=1)


def test_reserve_expected_version_mismatch(conn):
    r = _open(conn)
    pos = store.get_position(conn, r.position_id)
    with store.tx(conn):
        r2 = store.reserve(
            conn,
            ACCT,
            ReservePlan(
                intent="RESIZE_UP",
                round=0,
                qty=1,
                price=None,
                order_type="MARKET",
                position_id=pos.id,
                expected_version=pos.version + 1,
            ),
            max_positions=1,
            max_contracts=4,
        )
    assert not r2.ok and any("버전" in s for s in r2.reasons)


def test_exec_key_unique_and_next_attempt(conn):
    r = _open(conn)
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_leg(
            conn,
            position_id=r.position_id,
            account_id=ACCT,
            intent="OPEN",
            round=0,
            attempt=1,
            qty=1,
            price=None,
            order_type="MARKET",
        )
    assert store.next_attempt(conn, r.position_id, "OPEN", 0) == 2
    assert store.next_attempt(conn, r.position_id, "OPEN", 1) == 1


def test_record_fill_progress_incremental(conn):
    r = _open(conn, qty=3)
    assert store.record_fill_progress(conn, r.leg_id, 1) == 1
    assert store.record_fill_progress(conn, r.leg_id, 1) == 0
    assert store.get_position(conn, r.position_id).qty_held == 1
    assert store.record_fill_progress(conn, r.leg_id, 3) == 2
    leg = store.get_leg(conn, r.leg_id)
    assert leg.status == "filled" and leg.filled_qty == 3 and not leg.live
    assert store.get_position(conn, r.position_id).qty_held == 3
    with pytest.raises(ValueError):
        store.record_fill_progress(conn, r.leg_id, 2)  # 누적이 줄 수 없음
    with pytest.raises(ValueError):
        store.record_fill_progress(conn, r.leg_id, 4)  # 주문 초과


def test_decision_and_snapshots(conn):
    did = store.record_decision(
        conn,
        date="2026-10-27",
        account_id=ACCT,
        position_id=None,
        round=0,
        request_id="r1",
        intent="OPEN",
        direction="LONG",
        vehicle="FUTURES",
        target_leverage=1.5,
        data="{}",
    )
    assert did == 1
    with pytest.raises(sqlite3.IntegrityError):
        store.record_decision(
            conn,
            date="2026-10-27",
            account_id=ACCT,
            position_id=None,
            round=0,
            request_id="r2",
            intent="OPEN",
            direction="LONG",
            vehicle="FUTURES",
            data="{}",
        )
    store.write_nav_snapshot(
        conn,
        account_id=ACCT,
        date="2026-10-27",
        nav=Decimal("1"),
        cash=Decimal("2"),
        margin_total=Decimal("0"),
        add_margin=None,
    )
    store.write_nav_snapshot(
        conn,
        account_id=ACCT,
        date="2026-10-27",
        nav=Decimal("3"),
        cash=Decimal("2"),
        margin_total=Decimal("0"),
        add_margin=Decimal("0"),
    )
    row = conn.execute("SELECT nav, add_margin FROM fo_nav_snapshot").fetchone()
    assert (row["nav"], row["add_margin"]) == ("3", "0")
    rows = [
        {"code": "105W11", "kind": "FUTURES", "contract_side": "LONG", "qty": 2, "avg": "350"},
    ]
    store.write_positions_snapshot(conn, account_id=ACCT, date="2026-10-27", rows=rows)
    store.write_positions_snapshot(conn, account_id=ACCT, date="2026-10-27", rows=[])
    assert conn.execute("SELECT COUNT(*) FROM fo_positions_snapshot").fetchone()[0] == 0
