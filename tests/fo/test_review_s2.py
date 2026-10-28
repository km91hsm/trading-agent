"""Phase 26 단계 2 리뷰 반영(Codex S2-1~12·review R-1~4, Claude C2-1~10).

저장 계층: 트랜잭션 안 commit 금지(S2-1)·prepared 선점(S2-2)·상태 전이표 검증(C2-10)·예약 재검사가
보유·미체결 계획 손실을 받음(S2-7). 게이트: 대상 포지션 상태(C2-4)·감액 RESIZE 는 진입 조건
무관(S2-6)·보유 계획 손실 평균가(S2-7)·로컬 예약 차감(S2-8·C2-9)·저장 배수 단조(S2-9)·주문가능
조회는 호가단위 가격(C2-8). 실행기: HOLD 손절 강화 시 stop_fut(S2-10)·결정 RESIZE_DOWN 도 회수
→확정 경로(S2-5)·만료된 축소 다리 재발행(S2-4)·잔고 덮어쓰기 대신 체결 반영(S2-11·C2-1)·회수
선반영 체결의 경제값 1회(S2-3)·빈 entering 정리(C2-2)·잠금 밖 조회(S2-12)·조회 실패는 보류(C2-5)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from fo._gate import NAV, cfg, gate, orderable, portfolio, position
from fo._gate import decision as gdecision
from fo._gate import leg as gleg
from fo._gate import quote as gquote
from fo.test_executor import (
    ACCT,
    T0,
    TODAY,
    _held_position,
    account,
    balance,
    decision,
    env,
    holding,
    make_ctx,
    state,
)
from fo.test_positions import NOW, _bal, _open, _recover, _row, _state
from trader.broker.fake import FakeFoBroker, FakeMarket
from trader.broker.models import FoFill, FoOrderable, FoOrderRequest
from trader.fo import executor, positions, rules, sizing, store
from trader.fo import fills as fo_fills
from trader.fo.store import ReservePlan
from trader.ledger import repo

MULT = 50000


def _fill(order_no, *, side, filled, amount, cancelled=0):
    return FoFill(
        order_no=order_no, orgn_order_no=None, code="105W11", side=side, remaining_qty=0,
        filled_qty=filled, rejected_qty=0, cancelled_qty=cancelled,
        filled_amount=Decimal(amount) * MULT,  # 통화 금액(포인트 × 승수)
        avg_price=None, filled_at=T0,
    )  # fmt: skip


# --- 저장 계층 ---------------------------------------------------------------------------


def test_repo_insert_helpers_can_skip_commit(conn):
    """체결 동기화가 한 트랜잭션 안에서 부르는 insert_order/insert_fill 은 commit=False 로 원자성을
    지킨다(S2-1)."""
    with pytest.raises(RuntimeError), store.tx(conn):
        oid = repo.insert_order(
            conn, decision_id=None, account_id=ACCT, ticker="105W11", market="KR", side="BUY",
            qty=1, order_type="LIMIT", price=Decimal("350.5"), status="accepted", order_no="X",
            message="", mode="auto", asset="fo", commit=False,
        )  # fmt: skip
        repo.insert_fill(
            conn, order_id=oid, account_id=ACCT, qty=1, price=Decimal("350.5"), commit=False
        )
        assert conn.in_transaction
        raise RuntimeError("중단")
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0


def test_transition_validates_state_table(conn):
    """상태 전이는 `TRANSITIONS` 표 밖이면 ValueError, 예약은 사유로 거부(C2-10)."""
    pos = _held_position(conn, make_ctx(conn), qty=2, status="closing")
    with pytest.raises(ValueError):
        store.transition(conn, pos.id, expected_version=pos.version, status="resizing")
    assert store.get_position(conn, pos.id).status == "closing"
    with store.tx(conn):
        r = store.reserve(
            conn, ACCT,
            ReservePlan(intent="RESIZE_UP", round=0, qty=1, price=None, order_type="MARKET",
                        position_id=pos.id, expected_version=pos.version, new_status="resizing"),
            max_positions=1, max_contracts=4,
        )  # fmt: skip
    assert not r.ok and any("전이 불가" in s for s in r.reasons)


def test_claim_leg_only_from_prepared(conn):
    """회수가 cancelled 로 바꾼 prepared 다리는 전송하지 않는다(S2-2)."""
    ctx = make_ctx(conn)
    pos = _held_position(conn, ctx, qty=2)
    with store.tx(conn):
        lid = store.insert_leg(conn, position_id=pos.id, account_id=ACCT, intent="RESIZE_UP",
                               round=0, attempt=1, qty=1, price=Decimal("350.5"),
                               order_type="LIMIT")  # fmt: skip
        store.set_leg(conn, lid, status="cancelled")
    assert store.claim_leg(conn, lid) is False
    req = FoOrderRequest(code="105W11", side="BUY", qty=1, order_type="LIMIT",
                         price=Decimal("350.5"))  # fmt: skip
    out = executor._transmit(
        ctx, account(), leg_id=lid, position_id=pos.id, req=req, ref_price=Decimal("350.5"),
        mode="auto", stage="s", today=TODAY, intent="RESIZE_UP", extra={},
    )  # fmt: skip
    assert out["status"] == "recalled" and ctx.brokers[ACCT].orders == []
    assert store.get_leg(conn, lid).status == "cancelled"


def test_reserve_budget_uses_existing_planned_loss(conn):
    """예약 재검사: 보유·미체결 계획 손실(사이징이 평균가로 계산) + 이번 다리 × 단가 ≤ 한도
    (S2-7)."""
    pos = _held_position(conn, make_ctx(conn), qty=2)
    with store.tx(conn):
        r = store.reserve(
            conn, ACCT,
            ReservePlan(intent="RESIZE_UP", round=0, qty=1, price=None, order_type="MARKET",
                        position_id=pos.id, expected_version=pos.version, new_status="resizing",
                        planned_loss_per_contract=Decimal("105000"),
                        planned_loss_existing=Decimal("960000"),
                        budget_limit=Decimal("1000000")),
            max_positions=1, max_contracts=4,
        )  # fmt: skip
    assert not r.ok and any("수명 손실 예산" in s for s in r.reasons)


# --- 게이트 -----------------------------------------------------------------------------


def test_entry_rejected_on_target_position_status():
    """대상 포지션 자체가 closing·exit_failed·awaiting_settlement·unknown 이면 진입·증액 거부
    (C2-4)."""
    for st in ("closing", "exit_failed", "awaiting_settlement", "unknown"):
        pos = position(status=st, qty_held=2)
        v = rules.evaluate(
            gate(gdecision(intent="RESIZE", target_leverage=1.0), position=pos,
                 port=portfolio(positions=[pos]))  # fmt: skip
        )
        assert not v.approved and any(f"포지션 상태 {st}" in r for r in v.reasons), st


def test_resize_down_ignores_entry_only_rejections():
    """감액 RESIZE 는 enabled·검증·신선도·진입 창·킬스위치·halted 와 무관하게 RESIZE_DOWN 을
    만든다(S2-6). 시세 노후도 진입 조건이다."""
    pos = position(qty_held=3, qty_target=3, leverage_used=1.0)
    gi = gate(
        gdecision(intent="RESIZE", target_leverage=0.2), position=pos,
        port=portfolio(positions=[pos], verified={}, halted=True),
        cfg=cfg(enabled=False, vehicles=[]), inputs_fresh=False, killswitch=True,
        now=T0 + timedelta(hours=6),  # 16:30 KST — 진입 창 밖, 시세 노후
    )  # fmt: skip
    v = rules.evaluate(gi)
    assert v.approved and v.legs[0].intent == "RESIZE_DOWN" and v.legs[0].qty == 2, v.reasons
    assert "감액" in v.note and "진입 조건 무시" in v.note


def test_held_planned_loss_uses_average_price():
    """보유분 계획 손실은 평균체결가 기준(S2-7): 평균 350·손절 환산 340.4 ×2 = 96만 → r=1% 예산
    100만에 신규 0."""
    pos = position(qty_held=2, qty_target=2, leverage_used=1.0, stop_pt=Decimal("340"),
                   entry_premium=Decimal("350"))  # fmt: skip
    q = gquote(price="342.5", underlying=Decimal("342"))
    d = gdecision(intent="RESIZE", target_leverage=1.0, entry_price=342.5, stop_pt=340.0,
                  take_profit_pt=350.0)  # fmt: skip
    v = rules.evaluate(
        gate(d, position=pos, port=portfolio(positions=[pos]), quote=q,
             underlying=Decimal("342"), cfg=cfg(risk_per_trade_pct=0.01))  # fmt: skip
    )
    assert v.legs == [] and v.lifetime_budget["planned"] == Decimal("960000"), v.reasons
    assert v.planned_loss_existing == Decimal("960000")


def test_margin_allowance_subtracts_reserved_entry_contracts():
    """허용 신규 = floor(q × (1 − buffer)) − 로컬 예약 계약(S2-8). 축소 다리는 예약이 아니다
    (C2-9)."""
    n, reasons = sizing.margin_check(
        n=8, orderable=orderable(ord_qty=10), nav=NAV, local_reserved=Decimal(0), reserved_qty=3,
        buffer_pct=0.2, add_margin=Decimal(0), notional=Decimal("17500000"), margin_rate=0.12,
        rate_asof=TODAY, max_age_days=30, today=TODAY,
    )  # fmt: skip
    assert (n, reasons) == (5, [])
    legs = [
        gleg(id=1, intent="CLOSE", status="accepted", qty=2),
        gleg(id=2, intent="OPEN", status="accepted", qty=2, filled_qty=1),
    ]
    assert sizing.reserved_qty(legs) == 1
    assert sizing.local_reserved(legs, per_contract=Decimal("100")) == Decimal("100")


def test_stored_leverage_never_exceeds_approved_on_resize():
    """가격 상승으로 실제 노출 0.7 인 보유(승인 0.5)를 감액해도 저장 배수는 0.5 를 넘지 않는다
    (S2-9)."""
    pos = position(qty_held=4, qty_target=4, leverage_used=0.5)
    v = rules.evaluate(
        gate(gdecision(intent="RESIZE", target_leverage=0.5), position=pos,
             port=portfolio(positions=[pos]), cfg=cfg(max_contracts=10))  # fmt: skip
    )
    assert v.legs and v.legs[0].intent == "RESIZE_DOWN" and v.leverage_used <= 0.5


def test_orderable_gets_quantized_price(conn):
    """주문가능 조회에는 호가단위로 맞춘 가격을 보낸다(C2-8: 350.51 은 tick 0.02 밖)."""
    seen: list = []
    broker = FakeFoBroker(
        account(), balance=balance(), now=lambda: T0,
        orderable=FoOrderable(tot_qty=10, lqd_qty=0, ord_qty=10, cash=None, bass_idx=None),
        scenario=lambda name, *a: seen.append(a) if name == "orderable" else None,
    )  # fmt: skip
    ctx = make_ctx(conn, broker=broker)
    out = executor.run_leg(ctx, account(), env(decision(entry_price=350.51)))
    assert out[0]["status"] == "accepted"
    assert seen[0][2] != Decimal("350.51") and seen[0][2] == broker.orders[0].price


# --- 실행기 -----------------------------------------------------------------------------


def test_hold_tightening_stop_updates_stop_fut(conn):
    """HOLD 로 손절선을 강화하면 선물 환산 보호선도 함께(베이시스 0.5 − 슬리피지 0.1); 시세가 없으면
    기존 오프셋을 유지해 갱신(S2-10)."""
    ctx = make_ctx(conn)
    pos = _held_position(conn, ctx, qty=2, stop_fut=Decimal("345.4"))
    executor.run_leg(ctx, account(), env(decision(intent="HOLD", direction="LONG", stop_pt=347.0)))
    cur = store.get_position(conn, pos.id)
    assert (cur.stop_pt, cur.stop_fut) == (Decimal("347.0"), Decimal("347.4"))
    ctx2 = make_ctx(conn, market=FakeMarket(fail={"get_fo_price", "get_kospi200"}))
    executor.run_leg(ctx2, account(), env(decision(intent="HOLD", direction="LONG", stop_pt=348.0)))
    cur = store.get_position(conn, pos.id)
    assert (cur.stop_pt, cur.stop_fut) == (Decimal("348.0"), Decimal("348.4"))
    # 느슨한 손절은 둘 다 그대로
    executor.run_leg(ctx, account(), env(decision(intent="HOLD", direction="LONG", stop_pt=340.0)))
    cur = store.get_position(conn, pos.id)
    assert (cur.stop_pt, cur.stop_fut) == (Decimal("348.0"), Decimal("348.4"))


def test_resize_down_from_decision_recalls_entries_first(conn):
    """결정의 RESIZE_DOWN 도 공통 축소 경로: 살아 있는 진입 다리 회수 → 잔고 확정 → 보유 − 목표
    (S2-5)."""
    ctx = make_ctx(conn, max_contracts=10)
    broker = ctx.brokers[ACCT]
    pos = _held_position(conn, ctx, qty=3, qty_target=5, leverage_used=1.0, status="resizing")
    with store.tx(conn):
        lid = store.insert_leg(conn, position_id=pos.id, account_id=ACCT, intent="RESIZE_UP",
                               round=0, attempt=1, qty=2, price=Decimal("350.5"),
                               order_type="LIMIT")  # fmt: skip
        store.set_leg(conn, lid, status="accepted", order_no="E1")
    broker.states["E1"] = state("E1", order_qty=2, filled=0, remaining=0, cancelled=2,
                                terminal=True, st="cancelled")  # fmt: skip
    d = decision(intent="RESIZE", target_leverage=0.2)  # 목표 1
    out = executor.run_leg(ctx, account(), env(d, position_id=pos.id, expected_version=pos.version))
    assert broker.cancelled == [("E1", 0)]
    assert out[0]["intent"] == "RESIZE_DOWN" and out[0]["status"] == "accepted", out
    assert (broker.orders[-1].side, broker.orders[-1].qty) == ("SELL", 2)
    cur = store.get_position(conn, pos.id)
    assert cur.qty_target == 1 and store.get_leg(conn, lid).status == "cancelled"


def test_expired_reduce_leg_is_reissued_next_tick(conn):
    """만료된 CLOSE 다리가 있는 closing 포지션은 다음 틱에 잔량을 시장가로 다시 낸다(S2-4·C2-3)."""
    ctx = make_ctx(conn)
    broker = ctx.brokers[ACCT]
    pos = _held_position(conn, ctx, qty=2, code="205W11350", kind="CALL", vehicle="OPTION_BUY")
    assert executor.reduce(ctx, account(), pos, intent="CLOSE", qty=None, reason="t")["status"] == (
        "accepted"
    )
    cur = store.get_position(conn, pos.id)
    with store.tx(conn):
        store.set_leg(conn, cur.active_reduce_leg_id, status="expired")
    out2 = executor.continue_reduce(ctx, account(), cur)
    assert out2["status"] == "accepted" and len(broker.orders) == 2, out2
    assert (broker.orders[-1].order_type, broker.orders[-1].qty) == ("MARKET", 2)
    assert store.get_position(conn, pos.id).status == "closing"


def test_reduce_applies_fills_instead_of_overwriting_qty(conn):
    """체결 동기화 전에 다시 만난 완전 체결 CLOSE 다리는 체결을 다리에 반영해 closed 로 가고(unknown
    아님), 잔고 수량을 덮어쓰지 않아 뒤의 동기화가 이중 차감하지 않는다(S2-11·C2-1)."""
    ctx = make_ctx(conn)
    broker = ctx.brokers[ACCT]
    pos = _held_position(conn, ctx, qty=2, entry_premium=Decimal("350"))
    assert executor.reduce(ctx, account(), pos, intent="CLOSE", qty=None, reason="t")["status"] == (
        "accepted"
    )
    cur = store.get_position(conn, pos.id)
    leg = store.get_leg(conn, cur.active_reduce_leg_id)
    broker.balance = balance([])
    broker.fills[TODAY] = [_fill(leg.order_no, side="SELL", filled=2, amount="696")]  # 348 × 2
    out2 = executor.reduce(ctx, account(), cur, intent="STOP", qty=None, reason="손절")
    assert out2["status"] == "closed", out2
    cur = store.get_position(conn, pos.id)
    assert (cur.status, cur.qty_held) == ("closed", 0) and len(broker.orders) == 1
    r = fo_fills.sync(conn, broker, account(), TODAY, cfg=ctx.settings.fo, now=lambda: T0)
    cur = store.get_position(conn, pos.id)
    assert cur.qty_held == 0 and cur.realized_loss == Decimal(2) * 2 * MULT  # (350 − 348) × 2
    assert r["inserted"] + r["updated"] == 1


def test_recall_confirmed_fill_gets_economics_once_in_sync(conn):
    """회수 중 확정한 체결(record_fill_progress 선반영)도 동기화가 평균가·비용을 정확히 1회 적는다
    (S2-3·R-2)."""
    ctx = make_ctx(conn)
    broker = ctx.brokers[ACCT]
    pos = _held_position(conn, ctx, qty=2, status="resizing", qty_target=4)
    with store.tx(conn):
        lid = store.insert_leg(conn, position_id=pos.id, account_id=ACCT, intent="RESIZE_UP",
                               round=0, attempt=1, qty=2, price=Decimal("350.5"),
                               order_type="LIMIT")  # fmt: skip
        store.set_leg(conn, lid, status="accepted", order_no="E1", sent_at=T0.isoformat(),
                      accepted_at=T0.isoformat())  # fmt: skip
    broker.states["E1"] = state("E1", order_qty=2, filled=1, remaining=0, cancelled=1,
                                terminal=True, st="cancelled")  # fmt: skip
    broker.balance = balance([holding(qty=3)])
    rec = executor.recall_entries(ctx, account(), pos.id, reason="t")
    assert rec.confirmed_filled == 1 and store.get_position(conn, pos.id).qty_held == 3
    broker.fills[TODAY] = [_fill("E1", side="BUY", filled=1, amount="351", cancelled=1)]
    fo_fills.sync(conn, broker, account(), TODAY, cfg=ctx.settings.fo, now=lambda: T0)
    cur = store.get_position(conn, pos.id)
    assert cur.qty_held == 3 and cur.entry_premium == Decimal("351") and cur.fees > 0
    fo_fills.sync(conn, broker, account(), TODAY, cfg=ctx.settings.fo, now=lambda: T0)
    assert store.get_position(conn, pos.id).fees == cur.fees


def test_recall_cancel_closes_empty_entering(conn):
    """취소로 끝난 진입 다리는 빈 entering 포지션을 닫아 자리를 비운다(C2-2)."""
    ctx = make_ctx(conn)
    broker = ctx.brokers[ACCT]
    executor.run_leg(ctx, account(), env())
    pos = store.open_positions(conn, ACCT)[0]
    broker.states["FAKE-1"] = state("FAKE-1", order_qty=4, filled=0, remaining=0, cancelled=4,
                                    terminal=True, st="cancelled")  # fmt: skip
    rec = executor.recall_entries(ctx, account(), pos.id, reason="t")
    assert rec.cancelled == 1 and store.open_positions(conn, ACCT) == []
    assert store.get_position(conn, pos.id).status == "closed"


def test_order_state_queried_outside_write_lock(conn):
    """continue_reduce 의 확정 조회는 쓰기 잠금 밖(S2-12)."""
    ctx = make_ctx(conn)
    broker = ctx.brokers[ACCT]
    pos = _held_position(conn, ctx, qty=2)
    executor.reduce(ctx, account(), pos, intent="CLOSE", qty=None, reason="t")
    cur = store.get_position(conn, pos.id)
    leg = store.get_leg(conn, cur.active_reduce_leg_id)
    with store.tx(conn):
        store.set_leg(conn, leg.id, status="unknown")
    seen: list[bool] = []

    def hook(name, *a):
        if name == "get_order_state":
            seen.append(conn.in_transaction)
        return None

    broker.scenario = hook
    broker.states[leg.order_no] = state(leg.order_no, order_qty=2, filled=2, remaining=0,
                                        terminal=True, st="filled")  # fmt: skip
    broker.balance = balance([])
    out = executor.continue_reduce(ctx, account(), cur)
    assert seen == [False] and out["status"] == "closed", out


# --- 복구 -------------------------------------------------------------------------------


def test_recover_query_failure_defers_instead_of_unknown(conn):
    """주문 상태 조회 실패(네트워크)는 미확정 유지·보류이지 unknown 격상이 아니다(C2-5)."""
    pos, leg = _open(conn)
    store.set_leg(conn, leg.id, status="unknown", order_no="A1")
    rep = _recover(conn, _bal(), raise_state=True)
    assert rep.unknown == [] and rep.deferred == [leg.id]
    assert store.get_leg(conn, leg.id).status == "unknown"
    assert store.get_position(conn, pos.id).status == "entering"
    assert positions.has_blocking_state(conn, ACCT) == "미확정 다리 1건"


def test_cancelled_entry_leg_closes_empty_entering(conn):
    pos, leg = _open(conn)
    store.set_leg(conn, leg.id, status="sending", order_no="A1", sent_at=NOW.isoformat())
    _recover(conn, _bal(), states={"A1": _state("A1", order_qty=2, filled=0, cancelled=2,
                                                 state="cancelled")})  # fmt: skip
    assert store.get_position(conn, pos.id).status == "closed"
    assert store.open_positions(conn, ACCT) == []


def test_recover_releases_lock_between_broker_queries(conn):
    """복구 ①의 다리 확정은 앞 다리의 쓰기를 commit 한 뒤 다음 조회를 한다(S2-12)."""
    pos, leg = _open(conn)
    with store.tx(conn):
        store.set_leg(conn, leg.id, status="unknown", order_no="A1")
        lid2 = store.insert_leg(conn, position_id=pos.id, account_id=ACCT, intent="OPEN",
                                round=0, attempt=2, qty=1, price=Decimal("350.5"),
                                order_type="LIMIT")  # fmt: skip
        store.set_leg(conn, lid2, status="unknown", order_no="A2")
    flags: list[bool] = []

    def order_state_of(no, d):
        flags.append(conn.in_transaction)
        return _state(no, order_qty=2 if no == "A1" else 1, filled=2 if no == "A1" else 1)

    rep = positions.recover(conn, account_id=ACCT, balance=_bal(_row(qty=3)),
                            order_state_of=order_state_of, fills_of=lambda d: [], today=TODAY,
                            now=NOW)  # fmt: skip
    assert flags == [False, False] and rep.legs_fixed == 2
