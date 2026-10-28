"""실기 2 왕복 검증 모듈(`fo/order_test`, Phase 26): 가짜 브로커로 진입 → 잔고 확인 → 청산 순서."""

from __future__ import annotations

from decimal import Decimal

from helpers_sources import T0

from fo.test_executor import account, balance, holding, settings
from fo.test_inputs import _market, _master_with_chain
from trader.broker.fake import FakeFoBroker
from trader.broker.models import FoOrderState
from trader.fo import order_test

D = T0.date()


def _state(no, filled, *, terminal=True, st="filled"):
    return FoOrderState(order_no=no, order_qty=1, filled_qty=filled, remaining_qty=1 - filled,
                        rejected_qty=0, cancelled_qty=0 if filled else (1 if terminal else 0),
                        terminal=terminal, state=st)  # fmt: skip


def _broker(*, held, fill=True):
    acct = account()
    b = FakeFoBroker(acct, balance=balance(), now=lambda: T0)

    def scenario(name, *args):
        if name == "get_order_state":
            no = args[0]
            if fill:
                return _state(no, 1)
            cancelled = ("cancel_order" in b.calls)
            return _state(no, 0, terminal=cancelled, st="cancelled" if cancelled else "open")
        if name == "get_balance" and held is not None and len(b.orders) == 1:
            return balance([held])
        return None

    b.scenario = scenario
    return b


def _run(b, vehicle):
    return order_test.run(b, _market(), _master_with_chain(), settings(), vehicle, today=D,
                          wait_s=0, sleep=lambda s: None)  # fmt: skip


def test_futures_round_trip_ok():
    b = _broker(held=holding(qty=1))
    out = _run(b, "FUTURES")
    assert out["ok"], out
    assert [(o.side, o.order_type) for o in b.orders] == [("BUY", "MARKET"), ("SELL", "MARKET")]
    assert all(out["checks"].values())


def test_option_sell_uses_limit_bid_then_buy_back():
    code = order_test.pick_contract(_master_with_chain(), settings(), "OPTION_SELL",
                                    index=Decimal("350"), today=D).code  # fmt: skip
    b = _broker(held=holding(code=code, kind="CALL", side="SHORT", qty=1))
    out = _run(b, "OPTION_SELL")
    assert out["ok"], out
    assert out["code"] == code and code.startswith("205")
    entry, close = b.orders
    assert (entry.side, entry.order_type) == ("SELL", "LIMIT") and entry.price is not None
    assert close.side == "BUY"


def test_entry_unfilled_cancels_and_stops():
    b = _broker(held=None, fill=False)
    out = _run(b, "FUTURES")
    assert out["ok"] is False and "진입 미체결" in out["errors"][0]
    assert len(b.orders) == 1 and b.cancelled


def test_wrong_side_fails_but_still_closes():
    b = _broker(held=holding(qty=1, side="SHORT"))
    out = _run(b, "FUTURES")
    assert out["ok"] is False and out["checks"]["contract_side"] is False
    assert len(b.orders) == 2  # 확인 실패여도 청산은 낸다
