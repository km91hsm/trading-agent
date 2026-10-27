"""Phase 26 단계 2 Task 2.4: 게이트 — 대조 표·축소 다리 무관 조건·진입 거부 목록·배수·단조."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from fo._gate import (
    NOW,
    TODAY,
    cash,
    cfg,
    contract,
    decision,
    gate,
    leg,
    option_quote,
    orderable,
    portfolio,
    position,
    quote,
)
from trader.fo import rules
from trader.fo.schemas import FoDecision

# 스펙 §4.3 표: (국면, 전략가) → (LONG, SHORT) tilt 값; block 은 '역행' 칸만 0
TABLE = {
    ("bull", "RISK_ON"): (1.0, 0.5),
    ("bull", "NEUTRAL"): (1.0, 0.5),
    ("bull", "NONE"): (1.0, 0.5),
    ("bull", "RISK_OFF"): (0.5, 0.5),
    ("neutral", "NONE"): (1.0, 1.0),
    ("neutral", "NEUTRAL"): (1.0, 1.0),
    ("neutral", "RISK_ON"): (1.0, 0.5),
    ("neutral", "RISK_OFF"): (0.5, 1.0),
    ("bear", "RISK_OFF"): (0.5, 1.0),
    ("bear", "NEUTRAL"): (0.5, 1.0),
    ("bear", "NONE"): (0.5, 1.0),
    ("bear", "RISK_ON"): (0.5, 0.5),
}


@pytest.mark.parametrize("regime", ["bull", "neutral", "bear"])
@pytest.mark.parametrize("view", ["RISK_ON", "NEUTRAL", "RISK_OFF", "NONE"])
@pytest.mark.parametrize("mode", ["off", "tilt", "block"])
@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
@pytest.mark.parametrize("vehicle", ["FUTURES", "OPTION_BUY", "OPTION_SELL"])
def test_cross_factor_table(regime, view, mode, direction, vehicle):
    got = rules.cross_factor(regime, view, mode, direction, vehicle)
    against = rules.is_against_regime(regime, direction)
    if vehicle == "OPTION_SELL" and against:
        assert got == 0.0  # 옵션 매도 역행은 모드 무관 0
        assert (
            rules.cross_factor(regime, view, mode, direction, vehicle, sell_against_regime=True)
            != 0.0
            or mode == "block"
        )
        return
    if mode == "off":
        assert got == 1.0
        return
    exp = TABLE[(regime, view)][0 if direction == "LONG" else 1]
    if mode == "block" and against and view not in ("RISK_OFF" if regime == "bull" else "RISK_ON",):
        exp = 0.0  # 역행 칸: tilt 0.5 · block 0
    assert got == exp


def test_view_state_of():
    assert rules.view_state_of(None, today=TODAY, min_confidence=0.7) == "NONE"
    v = {"stance": "RISK_OFF", "confidence": 0.8, "date": "2026-10-27"}
    assert rules.view_state_of(v, today=TODAY, min_confidence=0.7) == "RISK_OFF"
    assert (
        rules.view_state_of({**v, "date": "2026-10-26"}, today=TODAY, min_confidence=0.7) == "NONE"
    )
    assert rules.view_state_of({**v, "confidence": 0.5}, today=TODAY, min_confidence=0.7) == "NONE"
    assert rules.view_state_of({**v, "stance": "WEIRD"}, today=TODAY, min_confidence=0.7) == "NONE"


def test_flat_requires_hold_or_close():
    with pytest.raises(ValueError):
        decision(intent="OPEN", direction="FLAT")
    with pytest.raises(ValueError):
        decision(intent="OPEN", stop_pt=None)
    with pytest.raises(ValueError):
        decision(intent="RESIZE", target_leverage=0)
    assert FoDecision(intent="HOLD", direction="FLAT", vehicle="FUTURES").intent == "HOLD"
    assert (
        decision(intent="CLOSE", direction="FLAT", stop_pt=None, target_leverage=0).direction
        == "FLAT"
    )


def test_open_approved_with_entry_leg():
    v = rules.evaluate(gate())
    assert v.approved, v.reasons
    assert len(v.legs) == 1 and v.legs[0].intent == "OPEN" and v.legs[0].order_side == "BUY"
    assert v.legs[0].order_type == "LIMIT" and v.legs[0].price == Decimal("350.50")
    assert v.n_target == v.legs[0].qty
    assert v.stop_pt == Decimal("345") and v.cross_factor == 1.0


def test_open_sizing_by_leverage():
    v = rules.evaluate(gate(decision(target_leverage=1.0)))
    assert v.approved and v.legs[0].qty == 4  # 1억 ÷ 1,750만 = 5 → max_contracts 4
    assert "계약 수 한도" in v.note
    v2 = rules.evaluate(gate(decision(target_leverage=0.5), cfg=cfg(max_contracts=10)))
    assert v2.approved and v2.legs[0].qty == 2  # 5천만 ÷ 1,750만 = 2
    v3 = rules.evaluate(gate(decision(target_leverage=0.1)))
    assert not v3.approved and "수량 0" in v3.reasons


def test_entry_rejections_collect_all():
    d = decision(target_leverage=2.0, confidence=0.3, vehicle="OPTION_SELL", premium_stop_mult=None)
    gi = gate(
        d,
        cfg=cfg(enabled=False, vehicles=["FUTURES"]),
        port=portfolio(
            verified={},
            halted=True,
            blocking="unknown 포지션 1건",
            cash=cash(add_margin=Decimal("1")),
        ),
        killswitch=True,
        inputs_fresh=False,
        historical=True,
        quote=None,
        orderable=None,
    )
    v = rules.evaluate(gi)
    assert not v.approved
    joined = " | ".join(v.reasons)
    for s in (
        "enabled",
        "허용 수단",
        "미검증",
        "입력 노후",
        "과거 asof",
        "halted",
        "킬스위치",
        "추가 증거금",
        "확신",
        "차단 상태",
        "premium_stop_mult",
        "주문가능",
        "시세 없음",
    ):
        assert s in joined, s
    assert len(v.reasons) >= 10


def test_historical_asof_rejects_entry():
    v = rules.evaluate(gate(historical=True))
    assert not v.approved and v.reasons == ["과거 asof — 신규 금지"]


def test_entry_window_and_quote_age():
    late = NOW.replace(hour=7)  # 16:00 KST
    assert "진입 창 밖" in rules.evaluate(gate(now=late)).reasons
    old = quote(quote_asof=NOW - timedelta(seconds=120))
    assert any("시세 노후" in r for r in rules.evaluate(gate(quote=old)).reasons)


def test_leverage_clamped_with_note():
    v = rules.evaluate(gate(decision(target_leverage=2.6), cfg=cfg(max_leverage=2.0)))
    assert v.approved and "배수 2.6→2.0" in v.note
    v2 = rules.evaluate(gate(decision(target_leverage=2.0), regime="bear", view_state="RISK_ON"))
    assert "대조 계수 0.5" in v2.note and v2.cross_factor == 0.5
    assert v2.approved and v2.leverage_used == 0.7  # bear+RISK_ON 조건부 허용, 4계약 실제 배수


def test_regime_cap_zero_rejects():
    v = rules.evaluate(gate(regime="bear", cfg=cfg(leverage_by_regime={"bear": 0})))
    assert any("국면 신규 금지" in r for r in v.reasons)
    # null 은 max_leverage
    v2 = rules.evaluate(
        gate(
            decision(target_leverage=2.0),
            regime="bear",
            cfg=cfg(leverage_by_regime={"bear": None}, cross_check="off"),
        )
    )
    assert v2.approved


def test_block_mode_against_regime_rejects():
    v = rules.evaluate(
        gate(
            decision(direction="SHORT", stop_pt=355.0, take_profit_pt=340.0),
            regime="bull",
            cfg=cfg(cross_check="block"),
        )
    )
    assert any("대조 차단" in r for r in v.reasons)


def test_option_sell_against_regime_zero_any_mode():
    d = decision(
        direction="LONG", vehicle="OPTION_SELL", premium_stop_mult=2.0, target_leverage=2.0
    )
    v = rules.evaluate(
        gate(d, regime="bear", cfg=cfg(cross_check="off"), quote=option_quote("PUT", "305W11350"))
    )
    assert any("대조 차단" in r for r in v.reasons)


def test_stop_sign_and_distance():
    v = rules.evaluate(gate(decision(stop_pt=349.8)))
    assert any("손절선" in r and "롱" in r for r in v.reasons)
    v2 = rules.evaluate(gate(decision(direction="SHORT", stop_pt=345.0, take_profit_pt=340.0)))
    assert any("숏" in r for r in v2.reasons)
    v3 = rules.evaluate(gate(decision(take_profit_pt=340.0)))
    assert any("익절선" in r for r in v3.reasons)


def test_stop_cannot_loosen_on_resize_and_hold():
    pos = position(stop_pt=Decimal("347"), leverage_used=2.0)
    d = decision(intent="RESIZE", target_leverage=2.0, stop_pt=344.0)
    v = rules.evaluate(gate(d, position=pos, port=portfolio(positions=[pos])))
    assert v.stop_pt == Decimal("347") and "손절선 불리한 이동 거부" in v.note
    v2 = rules.evaluate(
        gate(
            decision(intent="RESIZE", target_leverage=2.0, stop_pt=348.0),
            position=pos,
            port=portfolio(positions=[pos]),
        )
    )
    assert v2.stop_pt == Decimal("348")
    h = rules.evaluate(
        gate(
            FoDecision(intent="HOLD", direction="LONG", vehicle="FUTURES", stop_pt=340.0),
            position=pos,
        )
    )
    assert h.approved and h.legs == [] and h.stop_pt == Decimal("347")
    # 숏은 내리기만
    sp = position(
        direction="SHORT", contract_side="SHORT", stop_pt=Decimal("353"), leverage_used=2.0
    )
    s = rules.evaluate(
        gate(
            decision(
                intent="RESIZE",
                direction="SHORT",
                target_leverage=2.0,
                stop_pt=356.0,
                take_profit_pt=340.0,
            ),
            position=sp,
            port=portfolio(positions=[sp]),
        )
    )
    assert s.stop_pt == Decimal("353")


def test_leverage_and_approved_mult_cannot_rise():
    pos = position(leverage_used=0.5, qty_held=2, qty_target=2)
    v = rules.evaluate(
        gate(
            decision(intent="RESIZE", target_leverage=2.0),
            position=pos,
            port=portfolio(positions=[pos]),
        )
    )
    assert "배수 상승 금지" in v.note and v.legs == []  # 0.5 배수 = 2계약 = 보유 → 다리 없음
    assert v.leverage_used == 0.35  # 실제 노출(2계약 × 1,750만 ÷ 1억)
    sp = position(
        code="305W11350",
        kind="PUT",
        contract_side="SHORT",
        vehicle="OPTION_SELL",
        direction="LONG",
        approved_mult=1.6,
        leverage_used=2.0,
        premium_stop=Decimal("8.00"),
        entry_premium=Decimal("5"),
    )
    d = decision(intent="RESIZE", vehicle="OPTION_SELL", premium_stop_mult=2.5, target_leverage=2.0)
    v2 = rules.evaluate(
        gate(
            d,
            position=sp,
            port=portfolio(positions=[sp]),
            quote=option_quote("PUT", "305W11350", delta=0.5),
        )
    )
    assert v2.approved_mult == 1.6 and "보호 배수 상승 금지" in v2.note
    assert v2.premium_stop == Decimal("8.00")  # 새 후보 5×1.6=8.0 과 기존 8.0 의 min


def test_reduce_leg_ignores_enabled_vehicles_verified_freshness():
    """CLOSE 는 enabled·수단·검증·신선도·대조·배수·킬스위치와 무관하게 축소 다리를 만든다."""
    pos = position()
    gi = gate(
        decision(intent="CLOSE", direction="FLAT", stop_pt=None, target_leverage=0),
        cfg=cfg(enabled=False, vehicles=[]),
        port=portfolio(positions=[pos], verified={}, halted=True),
        position=pos,
        quote=None,
        inputs_fresh=False,
        historical=True,
        killswitch=True,
        regime="bear",
    )
    v = rules.evaluate(gi)
    assert v.approved and len(v.legs) == 1
    lg = v.legs[0]
    assert (lg.intent, lg.order_side, lg.qty, lg.order_type, lg.price) == (
        "CLOSE",
        "SELL",
        2,
        "MARKET",
        None,
    )
    # paused 는 기록만
    p = rules.evaluate(replace(gi, paused=True))
    assert not p.approved and any("일시정지" in r for r in p.reasons) and p.legs


def test_close_option_uses_best_quote_limit():
    sp = position(code="205W11350", kind="CALL", contract_side="LONG", vehicle="OPTION_BUY")
    v = rules.evaluate(
        gate(
            decision(intent="CLOSE", direction="FLAT", stop_pt=None, target_leverage=0),
            position=sp,
            port=portfolio(positions=[sp]),
            quote=option_quote("CALL", "205W11350", price="5.00"),
        )
    )
    assert v.legs[0].order_type == "LIMIT" and v.legs[0].price == Decimal("4.98")  # bid, tick 0.02
    assert v.legs[0].order_side == "SELL"


def test_close_without_position_or_unknown():
    assert not rules.evaluate(
        gate(decision(intent="CLOSE", direction="FLAT", stop_pt=None, target_leverage=0))
    ).approved
    up = position(status="unknown")
    v = rules.evaluate(
        gate(
            decision(intent="CLOSE", direction="FLAT", stop_pt=None, target_leverage=0),
            position=up,
            port=portfolio(positions=[up]),
        )
    )
    assert not v.approved and any("unknown" in r for r in v.reasons)


def test_resize_down_makes_reduce_leg():
    pos = position(qty_held=3, qty_target=3, leverage_used=1.0)
    d = decision(intent="RESIZE", target_leverage=0.2)  # 2천만 ÷ 1,750만 = 1 → 감액 2
    v = rules.evaluate(gate(d, position=pos, port=portfolio(positions=[pos])))
    assert v.approved and v.legs[0].intent == "RESIZE_DOWN" and v.legs[0].qty == 2
    assert v.legs[0].order_side == "SELL"


def test_opposite_and_closing_positions_block_entry():
    other = position(id=9, direction="SHORT", contract_side="SHORT")
    v = rules.evaluate(gate(port=portfolio(positions=[other])))
    assert any("반대 방향" in r for r in v.reasons)
    closing = position(id=9, status="closing")
    v2 = rules.evaluate(gate(port=portfolio(positions=[closing])))
    assert any("청산 중" in r for r in v2.reasons) and any("종목 수" in r for r in v2.reasons)
    # 다른 포지션의 살아 있는 다리만 있어도 종목 수 한도
    v3 = rules.evaluate(gate(port=portfolio(legs=[leg(position_id=9)])))
    assert any("종목 수" in r for r in v3.reasons)


def test_daily_loss_and_nav_none():
    v = rules.evaluate(gate(port=portfolio(realized_today=Decimal("-4000000"))))  # 3% = 300만
    assert any("당일 손실" in r for r in v.reasons)
    v2 = rules.evaluate(gate(port=portfolio(nav=None, cash=cash(nav=None))))
    assert "NAV 없음" in v2.reasons


def test_expiry_windows():
    v = rules.evaluate(gate(days_left=2))
    assert any("roll 창" in r for r in v.reasons)
    d = decision(vehicle="OPTION_BUY", target_leverage=2.0)
    v2 = rules.evaluate(gate(d, quote=option_quote(), days_left=4))
    assert any("잔존일" in r for r in v2.reasons)
    d3 = decision(
        vehicle="OPTION_SELL",
        direction="SHORT",
        stop_pt=356.0,
        take_profit_pt=340.0,
        premium_stop_mult=2.0,
        target_leverage=2.0,
    )
    v3 = rules.evaluate(gate(d3, quote=option_quote(), days_left=3))
    assert any("roll 창" in r for r in v3.reasons)  # 매도는 +1


def test_option_delta_invalid_rejects():
    d = decision(vehicle="OPTION_BUY", target_leverage=2.0)
    v = rules.evaluate(gate(d, quote=option_quote(delta=0.02)))
    assert any("델타 무효" in r for r in v.reasons)
    v2 = rules.evaluate(gate(d, quote=option_quote(delta=0.5, iv=None)))
    assert any("IV" in r for r in v2.reasons)


def test_option_sell_market_becomes_limit_at_bid():
    d = decision(
        vehicle="OPTION_SELL",
        direction="SHORT",
        stop_pt=356.0,
        take_profit_pt=340.0,
        premium_stop_mult=2.0,
        target_leverage=2.0,
        order_type="MARKET",
        entry_price=None,
    )
    q = option_quote("CALL", "205W11350", price="5.00", delta=0.5)
    v = rules.evaluate(
        gate(
            d,
            quote=q,
            contract=None,
            position=None,
            orderable=orderable(ord_qty=10, cash_=Decimal("8000000")),
        )
    )
    # 계약 코드는 contract 에서 — 없으면 거부
    assert any("계약 코드" in r for r in v.reasons)

    v2 = rules.evaluate(gate(d, quote=q, contract=contract("205W11350", "CALL", Decimal("350"))))
    assert v2.approved, v2.reasons
    lg = v2.legs[0]
    assert (lg.order_type, lg.price, lg.order_side, lg.kind) == (
        "LIMIT",
        Decimal("4.98"),
        "SELL",
        "CALL",
    )
    assert v2.premium_stop == Decimal("9.96") and v2.approved_mult == 2.0


def test_limit_price_clamped_or_rejected():
    """매수 지정가 > ask 는 ask 로 보정, 현재가보다 2% 넘게 아래면 괴리 거부."""
    v = rules.evaluate(gate(decision(entry_price=360.0)))
    assert v.approved and v.legs[0].price == Decimal("350.52")
    v2 = rules.evaluate(gate(decision(entry_price=340.0)))
    assert not v2.approved and any("지정가 괴리" in r for r in v2.reasons)
    # 옵션 결정에 선물 가격을 적는 실수: bid/ask 로 보정돼 사이징이 실제 프리미엄 기준
    d = decision(vehicle="OPTION_BUY", target_leverage=1.0, entry_price=350.5)
    v3 = rules.evaluate(
        gate(d, quote=option_quote(), contract=contract("205W11350", "CALL", Decimal("350")))
    )
    assert v3.approved and v3.legs[0].price == Decimal("5.02")
    assert v3.premium_stop == Decimal("2.510")
