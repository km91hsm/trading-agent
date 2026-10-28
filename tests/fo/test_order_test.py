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


# --- 단계 4 리뷰(Codex·Claude): 취소 확정·잔량만 재주문·정리 보장 -----------------------------


def _scripted(states, *, held=None, fail_balance=False, fail_close=False):
    """order_no → 상태 함수(취소 요청 여부를 받음). FAKE-n 은 n 번째 주문."""
    acct = account()
    b = FakeFoBroker(acct, balance=balance(), now=lambda: T0)

    def scenario(name, *args):
        if name == "get_order_state":
            no = args[0]
            return states[no]((no, 0) in b.cancelled)
        if name == "get_balance":
            if fail_balance:
                raise RuntimeError("잔고 조회 시간 초과")
            return balance([held]) if held is not None and len(b.orders) == 1 else None
        if name == "place_order" and fail_close and len(b.orders) == 2:
            raise RuntimeError("HTTP 500")
        return None

    b.scenario = scenario
    return b


def test_option_close_filled_during_cancel_no_extra_order():
    states = {
        "FAKE-1": lambda c: _state("FAKE-1", 1),
        # 청산 지정가: 취소 전엔 미체결, 취소 요청 뒤 확인하니 이미 1계약 체결(취소 실패)
        "FAKE-2": lambda c: _state("FAKE-2", 1) if c else _state("FAKE-2", 0, terminal=False,
                                                                st="open"),  # fmt: skip
    }
    b = _scripted(states, held=holding(code="205W11350", kind="CALL", qty=1))
    out = _run(b, "OPTION_BUY")
    assert out["ok"], out
    assert len(b.orders) == 2  # 시장가 재주문 없음(반대 포지션 방지)


def test_close_cancel_unconfirmed_does_not_resubmit():
    states = {
        "FAKE-1": lambda c: _state("FAKE-1", 1),
        "FAKE-2": lambda c: _state("FAKE-2", 0, terminal=False, st="open"),
    }
    b = _scripted(states, held=holding(code="205W11350", kind="CALL", qty=1))
    out = _run(b, "OPTION_BUY")
    assert out["ok"] is False and out["unresolved"] is True
    assert len(b.orders) == 2


def test_entry_cancel_unconfirmed_is_unresolved():
    states = {"FAKE-1": lambda c: _state("FAKE-1", 0, terminal=False, st="open")}
    b = _scripted(states)
    out = _run(b, "FUTURES")
    assert out["ok"] is False and out["unresolved"] is True and len(b.orders) == 1
    assert "취소 미확정" in out["errors"][0]


def test_balance_failure_still_closes():
    states = {"FAKE-1": lambda c: _state("FAKE-1", 1), "FAKE-2": lambda c: _state("FAKE-2", 1)}
    b = _scripted(states, fail_balance=True)
    out = _run(b, "FUTURES")
    assert out["ok"] is False and out["close"]["left"] == 0
    assert [o.side for o in b.orders] == ["BUY", "SELL"]


def test_close_order_exception_reported_unresolved():
    states = {"FAKE-1": lambda c: _state("FAKE-1", 1)}
    b = _scripted(states, held=holding(qty=1), fail_close=True)
    out = _run(b, "FUTURES")
    assert out["ok"] is False and out["unresolved"] is True
    assert any("청산 주문 오류" in e for e in out["errors"])
