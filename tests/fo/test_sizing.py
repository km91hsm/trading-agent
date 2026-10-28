"""Phase 26 단계 2 Task 2.5: 사이징.

계획 손실·보호선 단조·수명 예산·스트레스·증거금 버퍼·로컬 예약."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from fo._gate import (
    NAV,
    TODAY,
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
from trader.config import FoStressCfg
from trader.fo import rules, sizing
from trader.fo.sizing import StressItem


def test_futures_planned_loss_uses_basis_and_slippage():
    sf = sizing.stop_fut_of(Decimal("345"), Decimal("0.5"), "LONG", Decimal("0.1"))
    assert sf == Decimal("345.4")  # 345 + 0.5 − 0.1
    assert sizing.stop_fut_of(Decimal("355"), Decimal("0.5"), "SHORT", Decimal("0.1")) == Decimal(
        "355.6"
    )
    loss = sizing.planned_loss_per_contract(
        "FUTURES",
        "FUTURES",
        ref_price=Decimal("350.5"),
        stop_fut=sf,
        premium_stop=None,
        multiplier=50000,
    )
    assert loss == Decimal("255000.0")  # 5.1 × 5만
    with pytest.raises(ValueError):
        sizing.planned_loss_per_contract(
            "FUTURES",
            "FUTURES",
            ref_price=Decimal(1),
            stop_fut=None,
            premium_stop=None,
            multiplier=1,
        )


def test_option_planned_loss_from_premium_stop():
    sell = sizing.planned_loss_per_contract(
        "OPTION_SELL",
        "PUT",
        ref_price=Decimal("5"),
        stop_fut=None,
        premium_stop=Decimal("10"),
        multiplier=50000,
    )
    assert sell == Decimal("250000")
    buy = sizing.planned_loss_per_contract(
        "OPTION_BUY",
        "CALL",
        ref_price=Decimal("5"),
        stop_fut=None,
        premium_stop=Decimal("2.5"),
        multiplier=50000,
    )
    assert buy == Decimal("125000")
    full = sizing.planned_loss_per_contract(
        "OPTION_BUY",
        "CALL",
        ref_price=Decimal("5"),
        stop_fut=None,
        premium_stop=None,
        multiplier=50000,
    )
    assert full == Decimal("250000")


def test_premium_stop_monotone_after_add():
    """매도 2.0 → 새 후보 3.0 이면 2.0 유지(min); 매수 0.5 → 0.6 이면 0.6(max)."""
    assert sizing.premium_stop_merge(Decimal("2.0"), Decimal("3.0"), "OPTION_SELL") == Decimal(
        "2.0"
    )
    assert sizing.premium_stop_merge(Decimal("2.0"), Decimal("1.5"), "OPTION_SELL") == Decimal(
        "1.5"
    )
    assert sizing.premium_stop_merge(Decimal("0.5"), Decimal("0.6"), "OPTION_BUY") == Decimal("0.6")
    assert sizing.premium_stop_merge(None, Decimal("0.6"), "OPTION_BUY") == Decimal("0.6")
    assert sizing.premium_stop_candidate(
        "OPTION_SELL", Decimal("5"), approved_mult=2.0, buy_stop_pct=0.5
    ) == Decimal("10.0")
    assert sizing.premium_stop_candidate(
        "OPTION_BUY", Decimal("5"), approved_mult=None, buy_stop_pct=0.5
    ) == Decimal("2.5")


def test_lifetime_budget_counts_realized_and_no_recharge():
    pos = position(realized_loss=Decimal("150000"), fees=Decimal("10000"))
    b = sizing.lifetime_budget(
        pos, nav_at_open=NAV, r=0.002, held_loss=Decimal("30000"), pending_loss=Decimal("10000")
    )
    assert b["limit"] == Decimal("200000.000") and b["used"] == Decimal("160000") and b["ok"]
    b2 = sizing.lifetime_budget(
        pos, nav_at_open=NAV, r=0.002, held_loss=Decimal("30000"), pending_loss=Decimal("10001")
    )
    assert not b2["ok"]
    # 부분 손절 뒤 증액: 실현 190만 + 보유 1계약 25.5만 > 한도 200만 → 거부(재충전 없음)
    pos = position(realized_loss=Decimal("1900000"), fees=Decimal("0"))
    # 게이트: 부분 손절 뒤 증액 거부(예산 소진)
    gi = gate(
        decision(intent="RESIZE", target_leverage=2.0),
        position=replace(pos, qty_held=1, leverage_used=2.0),
        port=portfolio(positions=[pos]),
    )
    v = rules.evaluate(gi)
    assert any("수명 손실 예산" in r for r in v.reasons)


def test_total_basis_includes_live_legs():
    """목표 2 = 보유 1 + 살아 있는 다리 1 → 이번 다리 0."""
    pos = position(qty_held=1, qty_target=2, leverage_used=0.4)
    lg = leg(position_id=1, qty=1, filled_qty=0, status="accepted")
    v = rules.evaluate(
        gate(
            decision(intent="RESIZE", target_leverage=0.4),  # 4천만 ÷ 1,750만 = 2
            position=pos,
            port=portfolio(positions=[pos], legs=[lg]),
        )
    )
    assert v.approved and v.legs == [] and "다리 없음" in v.note


def test_stress_loss_worst_grid_option_sell_nonlinear():
    q = option_quote("CALL", price="5.00", delta=0.5, iv=18.0)
    linear = Decimal(str(abs(0.5) * 350 * 0.06)) * 50000  # 델타 선형 추정(6% 상승)
    worst = sizing.stress_loss([StressItem(q, -1, 16)], cfg=FoStressCfg(), multiplier=50000)
    assert worst > linear  # 감마·IV 상승으로 선형보다 크다
    fut = sizing.stress_loss([StressItem(quote(), 1, 16)], cfg=FoStressCfg(), multiplier=50000)
    assert fut == Decimal("350.5") * Decimal("0.06") * 50000  # 선물은 선형(6% 하락)


def test_overnight_cap_blocks_when_hold_overnight():
    """스트레스 한도(NAV × 1% = 100만) ÷ 계약당 최악 손실(6% × 350.5 × 5만 ≈ 105만) → 0계약."""
    v = rules.evaluate(gate(decision(target_leverage=1.0), cfg=cfg(overnight_risk_pct=0.01)))
    assert not v.approved and "스트레스 한도" in v.note
    v2 = rules.evaluate(
        gate(decision(target_leverage=1.0), cfg=cfg(hold_overnight=False, overnight_risk_pct=0.01))
    )
    assert v2.approved and v2.legs[0].qty == 4


def test_margin_buffer_from_ord_qty():
    n, reasons = sizing.margin_check(
        n=9,
        orderable=orderable(ord_qty=10),
        nav=NAV,
        local_reserved=Decimal(0),
        buffer_pct=0.3,
        add_margin=Decimal(0),
        notional=Decimal("17500000"),
        margin_rate=0.12,
        rate_asof=TODAY,
        max_age_days=30,
        today=TODAY,
    )
    # floor(10 × (1 − 0.3)) = 7 이지만 주문 뒤 잔여 현금도 NAV × 0.3 이상이어야 한다(단계 5 리뷰):
    # (현금 5천만 − 1억 × 0.3) ÷ 계약당 500만 = 4
    assert (n, reasons) == (4, [])


@pytest.mark.parametrize(
    "orderable_, add_margin, want",
    [
        (orderable(ord_qty=10, tot=12), Decimal(0), "청산가능분 섞임"),
        (orderable(ord_qty=10, lqd=1), Decimal(0), "청산가능 수량"),
        (orderable(ord_qty=0), Decimal(0), "주문가능 수량 0"),
        (orderable(ord_qty=10, cash_=None), Decimal(0), "기준 현금 없음"),
        (orderable(ord_qty=10), Decimal("5"), "추가 증거금"),
        (None, Decimal(0), "주문가능 조회 없음"),
    ],
)
def test_margin_premises_reject(orderable_, add_margin, want):
    n, reasons = sizing.margin_check(
        n=3,
        orderable=orderable_,
        nav=NAV,
        local_reserved=Decimal(0),
        buffer_pct=0.3,
        add_margin=add_margin,
        notional=Decimal("17500000"),
        margin_rate=0.12,
        rate_asof=TODAY,
        max_age_days=30,
        today=TODAY,
    )
    assert n == 0 and any(want in r for r in reasons)


def test_margin_rate_stale_and_sanity_and_buffer():
    base = dict(
        n=3,
        orderable=orderable(ord_qty=10),
        nav=NAV,
        local_reserved=Decimal(0),
        buffer_pct=0.3,
        add_margin=Decimal(0),
        notional=Decimal("17500000"),
        margin_rate=0.12,
        max_age_days=30,
        today=TODAY,
    )
    n, r = sizing.margin_check(**{**base, "rate_asof": date(2026, 9, 1)})
    assert n == 0 and any("증거금률 노후" in s for s in r)
    # 계약당 500만 ≤ 명목 × 0.12 × 3 = 630만 → 정합. 명목을 작게 주면 실패
    n2, r2 = sizing.margin_check(**{**base, "rate_asof": TODAY, "notional": Decimal("1000000")})
    assert n2 == 0 and any("정합성" in s for s in r2)
    # 현금 버퍼: 5000만 − 예약 2500만 = 2500만 < NAV × 0.3 = 3000만
    n3, r3 = sizing.margin_check(
        **{**base, "rate_asof": TODAY, "local_reserved": Decimal("25000000")}
    )
    assert n3 == 0 and any("현금 버퍼" in s for s in r3)


def test_local_reserved_includes_accepted_unfilled():
    legs = [
        leg(id=1, status="accepted", qty=2, filled_qty=1),
        leg(id=2, status="prepared", qty=1),
        leg(id=3, status="unknown", qty=1),
        leg(id=4, status="filled", qty=5, filled_qty=5),
        leg(id=5, status="cancelled", qty=3),
    ]
    assert sizing.local_reserved(legs, per_contract=Decimal("100")) == Decimal("300")


def test_max_contracts_total_and_option_caps():
    v = rules.evaluate(
        gate(
            decision(target_leverage=5.0),
            cfg=cfg(max_contracts=2, max_leverage=5.0, hold_overnight=False),
        )
    )
    assert v.approved and v.legs[0].qty == 2 and "계약 수 한도" in v.note
    d = decision(vehicle="OPTION_BUY", target_leverage=5.0)
    v2 = rules.evaluate(
        gate(
            d,
            quote=option_quote(price="5.00"),
            cfg=cfg(max_leverage=5.0, hold_overnight=False),
            contract=None,
        )
    )
    assert any("계약 코드" in r for r in v2.reasons)

    v3 = rules.evaluate(
        gate(
            d,
            quote=option_quote(price="5.00"),
            contract=contract("205W11350", "CALL", Decimal("350")),
            cfg=cfg(
                max_leverage=5.0,
                hold_overnight=False,
                max_contracts=10,
                option={"max_premium_pct": 0.01},
            ),
        )
    )
    # 전액 한도: NAV × 1% = 100만 ÷ (5.02 × 5만 = 25.1만) = 3
    assert v3.approved and v3.legs[0].qty == 3 and "옵션 매수 전액 한도" in v3.note


def test_delta_invalid_rejects_in_sizing_path():
    d = decision(vehicle="OPTION_BUY", target_leverage=2.0)
    v = rules.evaluate(gate(d, quote=option_quote(delta=1.5)))
    assert any("델타 무효" in r for r in v.reasons)
