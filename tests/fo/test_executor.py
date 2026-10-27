"""Phase 26 단계 2 Task 2.6: 실행기 — 진입은 실행 직전 전체 재계산, 축소는 체인·지수 실패에도 진행,
sending/unknown 재전송 없음, 진입 회수→확정→잔량 청산, exit_retry_max, 기록만 경로, 실전 skip."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from fo._gate import NAV, contract, option_quote, quote
from trader.broker.errors import UNKNOWN_RESPONSE_CODE, KisApiError
from trader.broker.fake import FakeFoBroker, FakeMarket
from trader.broker.fo import CONTRACT_VERSION
from trader.broker.models import (
    FoBalance,
    FoCash,
    FoFill,
    FoOrderable,
    FoOrderState,
    FoPosition,
    OrderResult,
)
from trader.config import AccountConfig, Settings
from trader.control import control
from trader.fo import executor, store
from trader.fo.schemas import FoDecision, FoDecisionEnvelope
from trader.orchestrator.run_store import market_dir, read_checkpoint

ACCT = "paper-fo"
T0 = datetime(2026, 10, 27, 1, 30, tzinfo=UTC)  # 10:30 KST
TODAY = date(2026, 10, 27)
MASTER = [contract("105W11", "FUTURES"), contract("205W11350", "CALL", Decimal("350"))]


class Spy:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str, *, escape: bool = True) -> bool:
        self.sent.append(text)
        return True


def account(env="paper", mode="auto", asset="fo") -> AccountConfig:
    return AccountConfig(
        id=ACCT if env == "paper" else "real-fo", env=env, cano="00000000", acnt_prdt_cd="03",
        markets=["KR"], mode=mode, asset=asset,
    )  # fmt: skip


def cash(nav=NAV, add_margin=Decimal(0), t=T0) -> FoCash:
    return FoCash(
        deposit=nav, orderable_cash=Decimal("50000000"), orderable_total=Decimal("50000000"),
        margin_total=Decimal(0), add_margin=add_margin, nav=nav, realized_today=Decimal(0),
        unrealized=Decimal(0), requested_at=t, received_at=t,
    )  # fmt: skip


def holding(code="105W11", kind="FUTURES", side="LONG", qty=2) -> FoPosition:
    return FoPosition(
        code=code, name=code, kind=kind, contract_side=side, qty=qty, avg_price=Decimal("350"),
        current_price=Decimal("350.5"), pnl=Decimal(0), sellable_qty=qty,
    )  # fmt: skip


def balance(positions=(), nav=NAV, **kw) -> FoBalance:
    return FoBalance(positions=list(positions), unverified=[], cash=cash(nav=nav, **kw))


def settings(**fo) -> Settings:
    base = {"enabled": True, "hold_overnight": False}
    base.update(fo)
    return Settings(fo=base)


def decision(**over) -> FoDecision:
    base = dict(
        intent="OPEN", direction="LONG", vehicle="FUTURES", target_leverage=1.0,
        order_type="LIMIT", entry_price=350.5, stop_pt=345.0, take_profit_pt=360.0,
        confidence=0.7, rationale="t", horizon_days=5,
    )  # fmt: skip
    base.update(over)
    return FoDecision(**base)


def env(d=None, **over) -> FoDecisionEnvelope:
    return FoDecisionEnvelope(decision=d or decision(), request_id="r1", **over)


def make_ctx(conn, *, broker=None, market=None, acct=None, dry_run=False, now=T0, **fo):
    acct = acct or account()
    broker = broker or FakeFoBroker(acct, balance=balance(), now=lambda: now,
                                    orderable=FoOrderable(tot_qty=10, lqd_qty=0, ord_qty=10,
                                                          cash=None, bass_idx=None))  # fmt: skip
    market = market or FakeMarket(fo_quotes={"105W11": quote(), "205W11350": option_quote()},
                                  kospi200=(Decimal("350"), now))  # fmt: skip
    s = settings(**fo)
    for v in ("FUTURES", "OPTION_BUY", "OPTION_SELL"):
        control.set_flag(conn, executor.verified_key(s, acct, v), CONTRACT_VERSION)
    return executor.FoExecContext(
        settings=s, accounts=[acct], conn=conn, brokers={acct.id: broker}, market_client=market,
        master=MASTER, notifier=Spy(), now=lambda: now, dry_run=dry_run,
    )  # fmt: skip


def ckpt(name: str):
    return read_checkpoint(market_dir(TODAY, "KR"), name)


def state(order_no, *, order_qty, filled, remaining, cancelled=0, terminal, st="open"):
    return FoOrderState(
        order_no=order_no, order_qty=order_qty, filled_qty=filled, remaining_qty=remaining,
        rejected_qty=0, cancelled_qty=cancelled, terminal=terminal, state=st,
    )  # fmt: skip


# --- 진입 -----------------------------------------------------------------------------


def test_entry_opens_position_and_records(conn):
    ctx = make_ctx(conn)
    out = (
        executor.run_leg(account(), ctx, env())
        if False
        else executor.run_leg(ctx, account(), env())
    )
    assert out[0]["status"] == "accepted" and out[0]["order_no"] == "FAKE-1"
    broker = ctx.brokers[ACCT]
    req = broker.orders[0]
    assert (req.code, req.side, req.qty, req.order_type, req.price) == (
        "105W11",
        "BUY",
        4,
        "LIMIT",
        Decimal("350.50"),
    )  # 1억 × 1.0 ÷ 1,750만 = 5 → max_contracts 4
    pos = store.open_positions(conn, ACCT)[0]
    assert (pos.status, pos.qty_target, pos.qty_held, pos.code, pos.stop_pt) == (
        "entering",
        4,
        0,
        "105W11",
        Decimal("345.0"),
    )
    assert pos.stop_fut == Decimal("345.4") and pos.nav_at_open == NAV
    leg = store.legs_of(conn, pos.id)[0]
    assert (leg.status, leg.order_no, leg.qty) == ("accepted", "FAKE-1", 4)
    row = conn.execute("SELECT asset, amount, decision_id, ticker FROM orders").fetchone()
    assert row["asset"] == "fo" and row["decision_id"] is None and row["ticker"] == "105W11"
    assert Decimal(row["amount"]) == Decimal("350.50") * 4 * 50000  # 승수
    assert ckpt(f"_fo_risk_check_{ACCT}")["data"]["verdict"]["approved"] is True
    assert ckpt(f"_fo_order_{ACCT}")["data"]["status"] == "accepted"
    assert any("파생 주문" in t and "4계약" in t for t in ctx.notifier.sent)


def test_entry_recomputes_and_shrinks_on_nav_drop(conn):
    """결정 때 NAV 1억이라도 실행 때 잔고가 5천만이면 수량이 절반(2)."""
    broker = FakeFoBroker(account(), balance=balance(nav=Decimal("50000000")), now=lambda: T0,
                          orderable=FoOrderable(tot_qty=10, lqd_qty=0, ord_qty=10, cash=None,
                                                bass_idx=None))  # fmt: skip
    ctx = make_ctx(conn, broker=broker)
    out = executor.run_leg(ctx, account(), env())
    assert out[0]["status"] == "accepted" and broker.orders[0].qty == 2


def test_entry_rejected_when_orderable_drops(conn):
    broker = FakeFoBroker(account(), balance=balance(), now=lambda: T0,
                          orderable=FoOrderable(tot_qty=0, lqd_qty=0, ord_qty=0, cash=None,
                                                bass_idx=None))  # fmt: skip
    ctx = make_ctx(conn, broker=broker)
    out = executor.run_leg(ctx, account(), env())
    assert out[0]["approved"] is False and any("주문가능 수량 0" in r for r in out[0]["reasons"])
    assert broker.orders == [] and store.open_positions(conn, ACCT) == []
    assert any("파생 판정 거부" in t for t in ctx.notifier.sent)


def test_stale_inputs_reject_entry(conn):
    old = T0 - timedelta(seconds=300)
    market = FakeMarket(fo_quotes={"105W11": quote(quote_asof=old, received_at=old)},
                        kospi200=(Decimal("350"), old))  # fmt: skip
    ctx = make_ctx(conn, market=market)
    out = executor.run_leg(ctx, account(), env())
    assert out[0]["approved"] is False and any("입력 노후" in r for r in out[0]["reasons"])


def test_option_sell_entry_market_becomes_limit(conn):
    d = decision(vehicle="OPTION_SELL", direction="SHORT", stop_pt=356.0, take_profit_pt=340.0,
                 premium_stop_mult=2.0, target_leverage=1.0, order_type="MARKET",
                 entry_price=None)  # fmt: skip
    ctx = make_ctx(conn)
    out = executor.run_leg(ctx, account(), env(d))
    assert out[0]["status"] == "accepted", out
    req = ctx.brokers[ACCT].orders[0]
    assert (req.code, req.side, req.order_type, req.price) == (
        "205W11350",
        "SELL",
        "LIMIT",
        Decimal("4.98"),
    )
    pos = store.open_positions(conn, ACCT)[0]
    assert pos.premium_stop == Decimal("9.96") and pos.entry_premium == Decimal("4.98")
    assert pos.vehicle == "OPTION_SELL" and pos.contract_side == "SHORT" and pos.kind == "CALL"


def test_rejected_releases_slot_next_attempt(conn):
    broker = FakeFoBroker(account(), balance=balance(), now=lambda: T0, fail={"place_order"},
                          orderable=FoOrderable(tot_qty=10, lqd_qty=0, ord_qty=10, cash=None,
                                                bass_idx=None))  # fmt: skip
    ctx = make_ctx(conn, broker=broker)
    out = executor.run_leg(ctx, account(), env())
    assert out[0]["status"] == "rejected"
    assert store.open_positions(conn, ACCT) == []  # entering·보유 0·다리 없음 → closed
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    broker.fail.clear()
    out2 = executor.run_leg(ctx, account(), env())
    assert out2[0]["status"] == "accepted"
    pid = out2[0]["position_id"]
    assert [x.attempt for x in store.legs_of(conn, pid)] == [1]  # 새 포지션이라 attempt 1


def test_unknown_kept_and_blocks_entry(conn):
    broker = FakeFoBroker(account(), balance=balance(), now=lambda: T0,
                          fail={"place_order:unknown"},
                          orderable=FoOrderable(tot_qty=10, lqd_qty=0, ord_qty=10, cash=None,
                                                bass_idx=None))  # fmt: skip
    ctx = make_ctx(conn, broker=broker)
    out = executor.run_leg(ctx, account(), env())
    assert out[0]["status"] == "unknown"
    pos = store.open_positions(conn, ACCT)[0]
    leg = store.legs_of(conn, pos.id)[0]
    assert leg.status == "unknown" and pos.status == "entering"
    assert conn.execute("SELECT status FROM orders").fetchone()[0] == "unknown"
    assert any("파생 주문 응답 없음" in t or "응답" in t for t in ctx.notifier.sent)
    broker.fail.clear()
    out2 = executor.run_leg(ctx, account(), env(decision(intent="RESIZE", target_leverage=1.0)))
    assert out2[0]["approved"] is False and any("미확정 다리" in r for r in out2[0]["reasons"])
    assert len(broker.orders) == 1  # 재전송 없음


def test_sending_leg_never_resent_after_restart(conn):
    """sending 으로 남은 다리(기록 중 죽음)는 place_order 를 다시 부르지 않고 상태 조회로 확정."""
    ctx = make_ctx(conn)
    broker = ctx.brokers[ACCT]
    executor.run_leg(ctx, account(), env())
    pos = store.open_positions(conn, ACCT)[0]
    leg = store.legs_of(conn, pos.id)[0]
    with store.tx(conn):
        store.set_leg(conn, leg.id, status="sending", order_no=None)
    broker.fills[TODAY] = [
        FoFill(order_no="FAKE-1", orgn_order_no=None, code="105W11", side="BUY", remaining_qty=0,
               filled_qty=4, rejected_qty=0, cancelled_qty=0, filled_amount=Decimal("70100000"),
               avg_price=Decimal("350.5"), filled_at=T0)
    ]  # fmt: skip
    broker.states["FAKE-1"] = state(
        "FAKE-1", order_qty=4, filled=4, remaining=0, terminal=True, st="filled"
    )
    from trader.fo import positions

    rep = positions.recover(conn, account_id=ACCT, balance=balance([holding(qty=4)]),
                            order_state_of=broker.get_order_state, fills_of=broker.get_fills,
                            today=TODAY, now=T0)  # fmt: skip
    conn.commit()
    assert rep.legs_fixed == 1 and len(broker.orders) == 1
    leg = store.get_leg(conn, leg.id)
    assert leg.status == "filled" and leg.order_no == "FAKE-1"
    assert executor.finalize(ctx, pos)["status"] == "held"


# --- 축소·회수 -----------------------------------------------------------------------


def _held_position(conn, ctx, qty=2, **over):
    """보유 중 포지션 1개를 원장에 만든다(브로커 잔고도 같은 수량)."""
    fields = dict(
        account_id=ACCT, code="105W11", kind="FUTURES", contract_side="LONG", direction="LONG",
        vehicle="FUTURES", status="held", qty_target=qty, qty_held=qty, leverage_used=0.35,
        stop_pt=Decimal("345"), nav_at_open=NAV, decision_date=TODAY.isoformat(),
    )  # fmt: skip
    fields.update(over)
    with store.tx(conn):
        pid = store.insert_position(conn, **fields)
    ctx.brokers[ACCT].balance = balance([holding(qty=qty, kind=fields["kind"],
                                                 code=fields["code"],
                                                 side=fields["contract_side"])])  # fmt: skip
    return store.get_position(conn, pid)


def test_reduce_leg_ignores_chain_failure(conn):
    """시세·지수가 다 실패해도 CLOSE 는 시장가 SELL 다리를 낸다."""
    market = FakeMarket(fail={"get_fo_price", "get_kospi200"})
    ctx = make_ctx(conn, market=market)
    pos = _held_position(conn, ctx)
    d = decision(intent="CLOSE", direction="FLAT", stop_pt=None, target_leverage=0)
    out = executor.run_leg(ctx, account(), env(d))
    assert out[0]["status"] == "accepted", out
    req = ctx.brokers[ACCT].orders[0]
    assert (req.side, req.qty, req.order_type, req.price) == ("SELL", 2, "MARKET", None)
    cur = store.get_position(conn, pos.id)
    assert cur.status == "closing" and cur.active_reduce_leg_id is not None


def test_recall_entries_before_stop_and_fill_during_cancel(conn):
    """미체결 진입 LIMIT + 손절 → 취소 요청 → 상태 조회(취소 중 1계약 체결) → 잔량 3 만 매도."""
    ctx = make_ctx(conn)
    broker = ctx.brokers[ACCT]
    pos = _held_position(conn, ctx, qty=2, status="resizing", qty_target=4)
    with store.tx(conn):
        leg_id = store.insert_leg(conn, position_id=pos.id, account_id=ACCT, intent="RESIZE_UP",
                                  round=0, attempt=1, qty=2, price=Decimal("350.5"),
                                  order_type="LIMIT")  # fmt: skip
        store.set_leg(conn, leg_id, status="accepted", order_no="E1")
    broker.states["E1"] = state("E1", order_qty=2, filled=1, remaining=0, cancelled=1,
                                terminal=True, st="cancelled")  # fmt: skip
    broker.balance = balance([holding(qty=3)])
    out = executor.reduce(ctx, account(), pos, intent="STOP", qty=None, reason="손절")
    assert broker.cancelled == [("E1", 0)]
    assert out["status"] == "accepted" and broker.orders[-1].qty == 3
    assert broker.orders[-1].side == "SELL" and broker.orders[-1].order_type == "MARKET"
    assert store.get_leg(conn, leg_id).status == "cancelled"
    cur = store.get_position(conn, pos.id)
    assert cur.qty_held == 3 and cur.status == "closing"
    assert any("파생 진입 회수" in t for t in ctx.notifier.sent)


def test_reduce_blocked_by_unknown_entry_leg(conn):
    ctx = make_ctx(conn)
    pos = _held_position(conn, ctx, qty=2, status="resizing", qty_target=4)
    with store.tx(conn):
        leg_id = store.insert_leg(conn, position_id=pos.id, account_id=ACCT, intent="RESIZE_UP",
                                  round=0, attempt=1, qty=2, price=Decimal("350.5"),
                                  order_type="LIMIT")  # fmt: skip
        store.set_leg(conn, leg_id, status="unknown")
    out = executor.reduce(ctx, account(), pos, intent="CLOSE", qty=None, reason="t")
    assert out["status"] == "blocked" and ctx.brokers[ACCT].orders == []


def test_exit_retry_max_to_exit_failed_critical(conn):
    """지정가 미체결 → 취소 확정 → 시장가 attempt+1 … 재시도 상한(2) 초과면 exit_failed + 알림."""
    ctx = make_ctx(conn, exit_retry_max=2)
    broker = ctx.brokers[ACCT]
    pos = _held_position(conn, ctx, qty=2, code="205W11350", kind="CALL", vehicle="OPTION_BUY")
    out = executor.reduce(ctx, account(), pos, intent="CLOSE", qty=None, reason="t")
    assert out["status"] == "accepted" and broker.orders[0].order_type == "LIMIT"
    n = 0
    for _ in range(3):
        n += 1
        leg = store.get_leg(conn, store.get_position(conn, pos.id).active_reduce_leg_id)
        broker.states[leg.order_no] = state(leg.order_no, order_qty=leg.qty, filled=0,
                                            remaining=0, cancelled=leg.qty, terminal=True,
                                            st="cancelled")  # fmt: skip
        out = executor.continue_reduce(ctx, account(), pos)
        assert out["status"] == "accepted"
    cur = store.get_position(conn, pos.id)
    assert cur.status == "exit_failed" and len(broker.orders) == 4
    assert all(o.order_type == "MARKET" for o in broker.orders[1:])
    assert any("파생 청산 실패" in t for t in ctx.notifier.sent)
    # 마지막 다리가 체결되면 closed
    leg = store.get_leg(conn, store.get_position(conn, pos.id).active_reduce_leg_id)
    broker.states[leg.order_no] = state(leg.order_no, order_qty=2, filled=2, remaining=0,
                                        terminal=True, st="filled")  # fmt: skip
    broker.balance = balance([])
    out = executor.continue_reduce(ctx, account(), pos)
    assert out["status"] == "closed" and store.open_positions(conn, ACCT) == []


def test_resize_down_from_decision(conn):
    ctx = make_ctx(conn, max_contracts=10)
    pos = _held_position(conn, ctx, qty=3, qty_target=3, leverage_used=1.0)
    d = decision(intent="RESIZE", target_leverage=0.2)  # 2천만 ÷ 1,750만 = 1 → 감액 2
    out = executor.run_leg(ctx, account(), env(d, position_id=pos.id, expected_version=pos.version))
    assert out[0]["status"] == "accepted" and out[0]["intent"] == "RESIZE_DOWN"
    req = ctx.brokers[ACCT].orders[0]
    assert (req.side, req.qty, req.order_type) == ("SELL", 2, "MARKET")
    cur = store.get_position(conn, pos.id)
    assert cur.status == "resizing" and cur.qty_target == 1


def test_expected_version_mismatch_rejects(conn):
    ctx = make_ctx(conn)
    pos = _held_position(conn, ctx)
    e = env(decision(intent="HOLD", direction="LONG"), position_id=pos.id, expected_version=99)
    out = executor.run_leg(ctx, account(), e)
    assert out[0]["approved"] is False and any("버전 불일치" in r for r in out[0]["reasons"])


def test_hold_applies_monotone_stop_only(conn):
    ctx = make_ctx(conn)
    pos = _held_position(conn, ctx)
    out = executor.run_leg(
        ctx, account(), env(decision(intent="HOLD", direction="LONG", stop_pt=347.0))
    )
    assert out[0]["legs"] == [] and store.get_position(conn, pos.id).stop_pt == Decimal("347.0")
    executor.run_leg(ctx, account(), env(decision(intent="HOLD", direction="LONG", stop_pt=340.0)))
    assert store.get_position(conn, pos.id).stop_pt == Decimal("347.0")
    assert ctx.brokers[ACCT].orders == []


def test_balance_mismatch_marks_unknown(conn):
    ctx = make_ctx(conn)
    pos = _held_position(conn, ctx, qty=2)
    ctx.brokers[ACCT].balance = balance([holding(qty=1)])  # 원장 2 > 브로커 1, 체결 없음
    out = executor.reduce(ctx, account(), pos, intent="CLOSE", qty=None, reason="t")
    assert out["status"] == "unknown" and store.get_position(conn, pos.id).status == "unknown"
    assert ctx.brokers[ACCT].orders == [] and any(
        "파생 포지션 불일치" in t for t in ctx.notifier.sent
    )


# --- 기록만·skip ----------------------------------------------------------------------


@pytest.mark.parametrize("how", ["dry_run", "paused", "off"])
def test_paused_off_dry_run_no_cancel_no_order(conn, how):
    ctx = make_ctx(conn, dry_run=how == "dry_run")
    if how == "paused":
        control.set_flag(conn, "paused", True)
    if how == "off":
        control.set_account_mode(conn, account(), "off")
    broker = ctx.brokers[ACCT]
    out = executor.run_leg(ctx, account(), env())
    assert out[0]["status"] == "recorded" and broker.orders == []
    assert store.open_positions(conn, ACCT) == []
    assert ckpt(f"_fo_order_{ACCT}")["data"]["record_only"] in ("dry_run", "paused", "mode=off")
    pos = _held_position(conn, ctx, qty=2, status="resizing", qty_target=4)
    with store.tx(conn):
        leg_id = store.insert_leg(conn, position_id=pos.id, account_id=ACCT, intent="RESIZE_UP",
                                  round=0, attempt=1, qty=2, price=Decimal("350.5"),
                                  order_type="LIMIT")  # fmt: skip
        store.set_leg(conn, leg_id, status="accepted", order_no="E1")
    r = executor.reduce(ctx, account(), pos, intent="STOP", qty=None, reason="손절")
    assert r["status"] == "recorded" and broker.cancelled == [] and broker.orders == []
    assert store.get_leg(conn, leg_id).status == "accepted"


def test_real_account_skipped(conn):
    real = account(env="real", mode="off")
    ctx = make_ctx(conn, acct=real)
    out = executor.run_leg(ctx, real, env())
    assert "skip" in out[0]["skipped"] and ctx.brokers[real.id].orders == []


def test_bookkeeping_failure_raises_and_keeps_sending(conn, monkeypatch):
    ctx = make_ctx(conn)
    from trader.ledger import repo

    def boom(*a, **k):
        raise RuntimeError("disk")

    monkeypatch.setattr(repo, "insert_order", boom)
    from trader.orchestrator.executor import OrderBookkeepingError

    with pytest.raises(OrderBookkeepingError):
        executor.run_leg(ctx, account(), env())
    pos = store.open_positions(conn, ACCT)[0]
    assert store.legs_of(conn, pos.id)[0].status == "sending"


def test_broker_rejected_response_marks_leg_rejected(conn):
    broker = FakeFoBroker(account(), balance=balance(), now=lambda: T0,
                          orderable=FoOrderable(tot_qty=10, lqd_qty=0, ord_qty=10, cash=None,
                                                bass_idx=None),
                          scenario=lambda name, *a: OrderResult(order_no=None, accepted=False,
                                                                message="거부")
                          if name == "place_order" else None)  # fmt: skip
    ctx = make_ctx(conn, broker=broker)
    out = executor.run_leg(ctx, account(), env())
    assert (
        out[0]["status"] == "rejected"
        and conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    )


def test_unknown_error_code_is_network_unknown():
    assert KisApiError(UNKNOWN_RESPONSE_CODE, "x").msg_cd == UNKNOWN_RESPONSE_CODE
