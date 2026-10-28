"""Phase 26 단계 2 PR(#60) GitHub Codex 리뷰 반영(G2-1~7).

G2-1 체결금액은 통화(원)라 포인트로 환산해 평균가·비용에 쓴다 · G2-2 회수는 prepared 를 조건부로만
취소(전송기가 선점했으면 pending) · G2-3 포지션 체결 반영은 오늘 전송한 다리만 · G2-4 평균가 가중은
경제값이 반영된 수량으로 · G2-5 목표가 보유+미체결보다 작으면 미체결 진입 회수 뒤 재계산 · G2-6 정산
대기 포지션은 반복 복구에서 유지 · G2-7 RESIZE_DOWN 재시도 소진은 resizing → exit_failed(목표
유지)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from fo._gate import decision as gdecision
from fo._gate import gate, portfolio, position
from fo._gate import leg as gleg
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
from fo.test_positions import _bal, _open, _recover
from trader.broker.models import FoFill
from trader.fo import executor, rules, store
from trader.fo import fills as fo_fills

MULT = 50000


def _fill(order_no, *, side, filled, remaining=0, points, cancelled=0):
    """KIS 체결 행: `filled_amount` 는 통화 금액(포인트 × 수량 × 승수)."""
    return FoFill(
        order_no=order_no, orgn_order_no=None, code="105W11", side=side, remaining_qty=remaining,
        filled_qty=filled, rejected_qty=0, cancelled_qty=cancelled,
        filled_amount=Decimal(points) * MULT, avg_price=None, filled_at=T0,
    )  # fmt: skip


def _entry_leg(conn, pos, *, qty, attempt=1, status="accepted", order_no=None):
    with store.tx(conn):
        lid = store.insert_leg(
            conn, position_id=pos.id, account_id=ACCT, intent="RESIZE_UP", round=0,
            attempt=attempt, qty=qty, price=Decimal("350.5"), order_type="LIMIT",
        )  # fmt: skip
        if status != "prepared":
            store.set_leg(
                conn, lid, status=status, order_no=order_no, sent_at=T0.isoformat(),
                accepted_at=T0.isoformat(),
            )  # fmt: skip
    return lid


def test_fill_amount_currency_converted_to_points(conn):
    """G2-1: 1계약 체결금액 17,500,000원 → 평균가 350pt, fills.price 350, 비용은 명목 × bp/2."""
    ctx = make_ctx(conn)
    broker = ctx.brokers[ACCT]
    executor.run_leg(ctx, account(), env())
    pos = store.open_positions(conn, ACCT)[0]
    leg = store.live_legs(conn, position_id=pos.id)[0]
    broker.fills[TODAY] = [_fill(leg.order_no, side="BUY", filled=1, remaining=leg.qty - 1,
                                 points="350")]  # fmt: skip
    fo_fills.sync(conn, broker, account(), TODAY, cfg=ctx.settings.fo, now=lambda: T0)
    cur = store.get_position(conn, pos.id)
    bp = Decimal(str(ctx.settings.fo.costs_bp["futures"]))
    assert cur.entry_premium == Decimal("350") and cur.qty_held == 1
    assert cur.fees == Decimal("350") * MULT * bp / 20000
    assert Decimal(conn.execute("SELECT price FROM fills").fetchone()["price"]) == Decimal("350")
    # 누적 2계약 35,050,000원 → 증분 351 → 평균 350.5
    broker.fills[TODAY] = [_fill(leg.order_no, side="BUY", filled=2, remaining=leg.qty - 2,
                                 points="701")]  # fmt: skip
    fo_fills.sync(conn, broker, account(), TODAY, cfg=ctx.settings.fo, now=lambda: T0)
    cur = store.get_position(conn, pos.id)
    assert cur.entry_premium == Decimal("350.5")
    assert Decimal(conn.execute("SELECT price FROM fills").fetchone()["price"]) == Decimal("350.5")


def test_recall_skips_prepared_leg_claimed_by_transmitter(conn, monkeypatch):
    """G2-2: 회수의 prepared 스냅샷이 낡아 전송기가 이미 sending 으로 선점했으면 cancelled 로
    덮지 않고 pending 으로 센다(포지션도 닫지 않음)."""
    ctx = make_ctx(conn)
    with store.tx(conn):
        pid = store.insert_position(
            conn, account_id=ACCT, code="105W11", kind="FUTURES", contract_side="LONG",
            direction="LONG", vehicle="FUTURES", status="entering", qty_target=2, qty_held=0,
            nav_at_open=Decimal("100000000"),
        )  # fmt: skip
    pos = store.get_position(conn, pid)
    lid = _entry_leg(conn, pos, qty=2, status="prepared")
    stale = store.live_legs(conn, position_id=pos.id)
    assert stale[0].status == "prepared"
    assert store.claim_leg(conn, lid)  # 전송기가 먼저 선점
    orig = store.live_legs

    def fake(c, **kw):
        if kw.get("position_id") == pos.id and "account_id" not in kw:
            monkeypatch.setattr(store, "live_legs", orig)
            return stale
        return orig(c, **kw)

    monkeypatch.setattr(store, "live_legs", fake)
    rr = executor.recall_entries(ctx, account(), pos.id, reason="t")
    assert (rr.cancelled, rr.pending) == (0, 1)
    assert store.get_leg(conn, lid).status == "sending"
    assert store.get_position(conn, pos.id).status == "entering"


def test_apply_position_fills_only_today_legs(conn):
    """G2-3: 어제 취소된 진입 다리(주문번호 1, 2 중 1 체결)와 오늘 CLOSE(주문번호 1, 2 체결)가 같은
    번호여도 오늘 다리에만 붙는다 → 보유 0(유령 1계약 없음)."""
    ctx = make_ctx(conn)
    broker = ctx.brokers[ACCT]
    pos = _held_position(conn, ctx, qty=2)
    y = (T0 - timedelta(days=1)).isoformat()
    with store.tx(conn):
        old = store.insert_leg(
            conn, position_id=pos.id, account_id=ACCT, intent="OPEN", round=0, attempt=1, qty=2,
            price=Decimal("350.5"), order_type="LIMIT",
        )  # fmt: skip
        store.set_leg(conn, old, status="cancelled", order_no="1", filled_qty=1, sent_at=y,
                      accepted_at=y)  # fmt: skip
        conn.execute("UPDATE fo_order_legs SET created_at = ? WHERE id = ?", (y, old))
        new = store.insert_leg(conn, position_id=pos.id, account_id=ACCT, intent="CLOSE", round=0,
                               attempt=1, qty=2, price=None, order_type="MARKET")  # fmt: skip
        store.set_leg(conn, new, status="accepted", order_no="1", sent_at=T0.isoformat(),
                      accepted_at=T0.isoformat())  # fmt: skip
    broker.fills[TODAY] = [_fill("1", side="SELL", filled=2, points="700")]
    assert executor._apply_position_fills(ctx, broker, pos, TODAY)
    assert store.get_leg(conn, old).filled_qty == 1
    assert store.get_leg(conn, new).filled_qty == 2
    assert store.get_position(conn, pos.id).qty_held == 0


def test_entry_average_weights_only_accounted_fills(conn):
    """G2-4: 복구가 두 진입 다리(1@351·1@352)의 수량을 먼저 반영한 뒤 동기화해도 평균가는
    (350×2 + 351 + 352) ÷ 4 = 350.75(350.6875 아님)."""
    ctx = make_ctx(conn, max_contracts=10)
    broker = ctx.brokers[ACCT]
    pos = _held_position(conn, ctx, qty=2, entry_premium=Decimal("350"), status="resizing",
                         qty_target=4)  # fmt: skip
    a = _entry_leg(conn, pos, qty=1, attempt=1, order_no="A")
    b = _entry_leg(conn, pos, qty=1, attempt=2, order_no="B")
    with store.tx(conn):
        store.record_fill_progress(conn, a, 1)
        store.record_fill_progress(conn, b, 1)
    assert store.get_position(conn, pos.id).qty_held == 4
    broker.fills[TODAY] = [_fill("A", side="BUY", filled=1, points="351"),
                           _fill("B", side="BUY", filled=1, points="352")]  # fmt: skip
    fo_fills.sync(conn, broker, account(), TODAY, cfg=ctx.settings.fo, now=lambda: T0)
    cur = store.get_position(conn, pos.id)
    assert (cur.qty_held, cur.entry_premium) == (4, Decimal("350.75"))


def test_resize_below_pending_requests_recall():
    """G2-5(게이트): 보유 1 + 미체결 3, 목표 2 → 다리 없이 `recall_entries` 요청(승인)."""
    pos = position(qty_held=1, qty_target=4, leverage_used=1.0)
    pend = gleg(position_id=pos.id, intent="RESIZE_UP", qty=3, status="accepted")
    v = rules.evaluate(
        gate(gdecision(intent="RESIZE", target_leverage=0.4), position=pos,
             port=portfolio(positions=[pos], legs=[pend]))  # fmt: skip
    )
    assert v.approved and v.legs == [] and v.recall_entries, v.reasons
    assert "회수" in v.note


def test_resize_recalls_excess_pending_then_resizes(conn):
    """G2-5(실행기): 미체결 3을 회수(취소 확정)한 뒤 다시 계산해 목표 2 − 보유 1 = 1계약 증액."""
    ctx = make_ctx(conn, max_contracts=10)
    broker = ctx.brokers[ACCT]
    pos = _held_position(conn, ctx, qty=1, qty_target=4, leverage_used=1.0, status="resizing")
    lid = _entry_leg(conn, pos, qty=3, order_no="E1")
    broker.states["E1"] = state("E1", order_qty=3, filled=0, remaining=0, cancelled=3,
                                terminal=True, st="cancelled")  # fmt: skip
    d = decision(intent="RESIZE", target_leverage=0.4)  # 목표 2
    out = executor.run_leg(ctx, account(), env(d, position_id=pos.id, expected_version=pos.version))
    assert broker.cancelled == [("E1", 0)]
    assert store.get_leg(conn, lid).status == "cancelled"
    assert out[0]["intent"] == "RESIZE_UP" and out[0]["status"] == "accepted", out
    assert (broker.orders[-1].side, broker.orders[-1].qty) == ("BUY", 1)
    assert store.get_position(conn, pos.id).qty_target == 2


def test_recover_keeps_awaiting_settlement_on_repeat(conn):
    """G2-6: 정산 대기로 간 포지션은 같은 잔고로 다시 복구해도 unknown 이 아니라 그대로."""
    pos, leg = _open(conn, last_trade_date="2026-10-22")
    store.set_leg(conn, leg.id, status="filled", order_no="A1")
    store.record_fill_progress(conn, leg.id, 2)
    p = store.get_position(conn, pos.id)
    store.transition(conn, p.id, expected_version=p.version, status="held")
    assert _recover(conn, _bal()).settling == [pos.id]
    rep = _recover(conn, _bal())
    assert rep.unknown == [] and rep.settling == [pos.id]
    assert store.get_position(conn, pos.id).status == "awaiting_settlement"


def test_resize_down_retry_exhaustion_escalates_to_exit_failed(conn):
    """G2-7: 감액 재시도 소진은 ValueError 없이 resizing → exit_failed(목표 1 유지, critical 알림),
    마지막 다리가 체결되면 held(보유 1)."""
    ctx = make_ctx(conn, exit_retry_max=2, max_contracts=10)
    broker = ctx.brokers[ACCT]
    pos = _held_position(conn, ctx, qty=3, qty_target=3, leverage_used=1.0)
    d = decision(intent="RESIZE", target_leverage=0.2)  # 목표 1 → 감액 2
    out = executor.run_leg(ctx, account(), env(d, position_id=pos.id, expected_version=pos.version))
    assert out[0]["status"] == "accepted", out
    for _ in range(3):
        cur = store.get_position(conn, pos.id)
        leg = store.get_leg(conn, cur.active_reduce_leg_id)
        broker.states[leg.order_no] = state(leg.order_no, order_qty=leg.qty, filled=0,
                                            remaining=0, cancelled=leg.qty, terminal=True,
                                            st="cancelled")  # fmt: skip
        out = executor.continue_reduce(ctx, account(), cur)
        assert out["status"] == "accepted", out
    cur = store.get_position(conn, pos.id)
    assert (cur.status, cur.qty_target, len(broker.orders)) == ("exit_failed", 1, 4)
    assert all((o.side, o.qty) == ("SELL", 2) for o in broker.orders)
    assert any("파생 청산 실패" in t for t in ctx.notifier.sent)
    leg = store.get_leg(conn, cur.active_reduce_leg_id)
    broker.states[leg.order_no] = state(leg.order_no, order_qty=2, filled=2, remaining=0,
                                        terminal=True, st="filled")  # fmt: skip
    broker.balance = balance([holding(qty=1)])
    out = executor.continue_reduce(ctx, account(), cur)
    assert out["status"] == "held", out
    assert store.get_position(conn, pos.id).qty_held == 1
