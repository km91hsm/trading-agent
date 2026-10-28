"""Phase 26 Task 4.7: 파생 포지션 수명 E2E — 6가지 방향·수단 조합마다 진입(부분체결) → 보유 →
재평가 감액 → 손절 → 청산, 그리고 응답 없는 다리 → 재시작 복구 → 보유, 만기 → 정산.
가짜 브로커·가짜 시세 + 실제 executor·engine·reeval·fills 경로."""

from __future__ import annotations

from decimal import Decimal

import pytest
from orchestrator.test_fills_fo import fill

from fo._gate import contract, option_quote, quote
from fo.test_executor import (
    ACCT,
    MASTER,
    T0,
    TODAY,
    _held_position,
    balance,
    decision,
    env,
    holding,
    make_ctx,
    state,
)
from trader.broker.fake import FakeMarket
from trader.fo import engine, executor, reeval, store
from trader.fo import fills as fo_fills
from trader.fo.schemas import contract_side_of, kind_of, order_side

MASTER2 = [*MASTER, contract("305W11350", "PUT", Decimal("350"))]
CASES = [
    ("FUTURES", "LONG"), ("FUTURES", "SHORT"), ("OPTION_BUY", "LONG"), ("OPTION_BUY", "SHORT"),
    ("OPTION_SELL", "LONG"), ("OPTION_SELL", "SHORT"),
]  # fmt: skip


def _market(idx="350"):
    return FakeMarket(
        fo_quotes={"105W11": quote(), "205W11350": option_quote(),
                   "305W11350": option_quote(kind="PUT", code="305W11350")},
        kospi200=(Decimal(idx), T0),
    )  # fmt: skip


def _decision(vehicle, direction, **over):
    long_ = direction == "LONG"
    base = dict(vehicle=vehicle, direction=direction, stop_pt=345.0 if long_ else 355.0,
                take_profit_pt=360.0 if long_ else 340.0, target_leverage=1.0)  # fmt: skip
    if vehicle == "FUTURES":
        base.update(order_type="LIMIT", entry_price=350.5)
    else:
        base.update(order_type="MARKET", entry_price=None)
    if vehicle == "OPTION_SELL":
        base["premium_stop_mult"] = 2.0
    base.update(over)
    return decision(**base)


def _sync(ctx, fills):
    ctx.brokers[ACCT].fills[TODAY] = fills
    return fo_fills.sync(ctx.conn, ctx.brokers[ACCT], executor.account_of(ctx, ACCT), TODAY,
                         cfg=ctx.settings.fo, now=ctx.now)  # fmt: skip


def _px(req):
    return req.price if req.price is not None else Decimal("5.00")


def _tick(ctx):
    return engine.tick(ctx, request_reeval=lambda c, p: None)


@pytest.mark.parametrize(("vehicle", "direction"), CASES)
def test_lifecycle(conn, vehicle, direction):
    ctx = make_ctx(conn, market=_market(), max_contracts=10, reeval={"periodic_max_per_day": 0})
    ctx.master = MASTER2
    broker = ctx.brokers[ACCT]
    acct = executor.account_of(ctx, ACCT)
    cs, kind = contract_side_of(direction, vehicle), kind_of(direction, vehicle)
    entry_side, exit_side = order_side(cs, entry=True), order_side(cs, entry=False)

    # ① 아침 결정 → 진입 다리
    out = executor.run_leg(ctx, acct, env(_decision(vehicle, direction)))
    assert out[0]["status"] == "accepted", out
    req = broker.orders[0]
    assert req.side == entry_side
    pos = store.open_positions(conn, ACCT)[0]
    assert (pos.contract_side, pos.kind, pos.status) == (cs, kind, "entering")
    n = req.qty
    assert n >= 2
    code, px = req.code, _px(req)
    ps0 = pos.premium_stop

    # ② 부분체결 → 전량 체결 → 엔진이 보유로
    _sync(ctx, [fill("FAKE-1", filled=1, amount=px, remaining=n - 1, side=entry_side, code=code)])
    assert store.get_position(conn, pos.id).qty_held == 1
    _sync(ctx, [fill("FAKE-1", filled=n, amount=px * n, remaining=0, side=entry_side, code=code)])
    broker.balance = balance([holding(code=code, kind=kind, side=cs, qty=n)])
    _tick(ctx)
    pos = store.get_position(conn, pos.id)
    assert (pos.status, pos.qty_held) == ("held", n)

    # ③ 재평가 감액(목표 1계약)
    small = 0.2 if vehicle == "FUTURES" else 0.1
    e = env(_decision(vehicle, direction, intent="RESIZE", target_leverage=small),
            account_id=ACCT, position_id=pos.id, expected_version=pos.version)  # fmt: skip
    res = reeval.apply(ctx, e, round_n=1)
    assert res["applied"] == "RESIZE" and res["results"][0]["intent"] == "RESIZE_DOWN", res
    down = broker.orders[-1]
    assert (down.side, down.qty) == (exit_side, n - 1)
    _sync(ctx, [fill("FAKE-1", filled=n, amount=px * n, remaining=0, side=entry_side, code=code),
                fill("FAKE-2", filled=n - 1, amount=px * (n - 1), remaining=0, side=exit_side,
                     code=code)])  # fmt: skip
    broker.balance = balance([holding(code=code, kind=kind, side=cs, qty=1)])
    _tick(ctx)
    pos = store.get_position(conn, pos.id)
    assert (pos.status, pos.qty_held, pos.qty_target) == ("held", 1, 1)
    if ps0 is not None:  # 옵션 보호선은 느슨해지지 않는다
        assert pos.premium_stop is not None
        assert (pos.premium_stop <= ps0) if vehicle == "OPTION_SELL" else (pos.premium_stop >= ps0)

    # ④ 기초지수가 손절선을 넘는다 → 엔진 손절 → 청산 체결 → closed
    ctx.market_client = _market("344" if direction == "LONG" else "356")
    out = _tick(ctx)
    exits = out["accounts"][ACCT]["exits"]
    assert exits and exits[0]["intent"] == "STOP", out
    stop = broker.orders[-1]
    assert (stop.side, stop.qty) == (exit_side, 1)
    _sync(ctx, [fill("FAKE-1", filled=n, amount=px * n, remaining=0, side=entry_side, code=code),
                fill("FAKE-2", filled=n - 1, amount=px * (n - 1), remaining=0, side=exit_side,
                     code=code),
                fill("FAKE-3", filled=1, amount=px, remaining=0, side=exit_side, code=code)])
    broker.balance = balance()
    _tick(ctx)
    pos = store.get_position(conn, pos.id)
    assert (pos.status, pos.qty_held) == ("closed", 0)
    rows = conn.execute("SELECT asset, side FROM orders WHERE account_id = ? ORDER BY id",
                        (ACCT,)).fetchall()  # fmt: skip
    assert [r["asset"] for r in rows] == ["fo"] * 3
    assert [r["side"] for r in rows] == [entry_side, exit_side, exit_side]


def test_unknown_leg_restart_recovers_to_held(conn):
    from trader.broker.fake import FakeFoBroker
    from trader.broker.models import FoOrderable

    acct = executor.account_of(make_ctx(conn), ACCT)
    broker = FakeFoBroker(acct, balance=balance(), now=lambda: T0, fail={"place_order:unknown"},
                          orderable=FoOrderable(tot_qty=10, lqd_qty=0, ord_qty=10, cash=None,
                                                bass_idx=None))  # fmt: skip
    ctx = make_ctx(conn, broker=broker, market=_market(), reeval={"periodic_max_per_day": 0})
    out = executor.run_leg(ctx, acct, env(_decision("FUTURES", "LONG")))
    assert out[0]["status"] == "unknown"
    leg = store.live_legs(conn, account_id=ACCT)[0]
    pos = store.get_position(conn, leg.position_id)
    assert leg.status == "unknown" and leg.order_no is None
    # 재시작: 새 프로세스의 컨텍스트, 체결 조회에 그 주문이 정확히 한 건 있다
    broker.fail = set()
    broker.fills[TODAY] = [fill("ODN9", filled=leg.qty, amount=Decimal("350.5") * leg.qty,
                                remaining=0, side="BUY")]  # fmt: skip
    broker.states["ODN9"] = state("ODN9", order_qty=leg.qty, filled=leg.qty, remaining=0,
                                  terminal=True, st="filled")  # fmt: skip
    broker.balance = balance([holding(qty=leg.qty)])
    ctx2 = make_ctx(conn, broker=broker, market=_market(), reeval={"periodic_max_per_day": 0})
    _tick(ctx2)
    cur = store.get_position(conn, pos.id)
    assert (cur.status, cur.qty_held) == ("held", leg.qty)
    assert store.get_leg(conn, leg.id).order_no == "ODN9"


def test_option_expiry_settles_next_day(conn):
    ctx = make_ctx(conn, market=_market(), reeval={"periodic_max_per_day": 0})
    pos = _held_position(conn, ctx, qty=1, code="205W11350", kind="CALL", vehicle="OPTION_BUY",
                         last_trade_date="2026-10-26", entry_premium=Decimal("5"))  # fmt: skip
    ctx.brokers[ACCT].balance = balance()  # 만기 뒤 잔고에서 사라짐(체결 없음)
    _tick(ctx)
    cur = store.get_position(conn, pos.id)
    assert cur.status == "settled"  # 복구가 정산 대기로 옮기고 같은 틱의 정산 확인이 닫는다
