from datetime import date, timedelta
from decimal import Decimal

from helpers_agents import ok_decision

from trader.agents.schemas import Decision
from trader.broker.models import Position
from trader.config import RiskLimits
from trader.risk import costs, rules, stance

D = date(2026, 10, 9)
LIMITS = RiskLimits()


def _pos(ticker="000660", qty=10, price="100000"):
    return Position(
        ticker=ticker,
        market="KR",
        qty=qty,
        avg_price=Decimal(price),
        current_price=Decimal(price),
        eval_amount=Decimal(price) * qty,
        pnl_pct=0.0,
    )


def _portfolio(**over) -> rules.Portfolio:
    base = dict(
        account_id="paper-main",
        market="KR",
        nav=Decimal("10000000"),
        cash_available=Decimal("10000000"),
        positions={},
        today_pnl_pct=0.0,
        today_buy_tickers=set(),
        today_order_amount=Decimal(0),
        held_since={},
        trades_this_week={},
    )
    base.update(over)
    return rules.Portfolio(**base)


def _eval(decision: dict, portfolio=None, *, price="70000", market="KR", st="normal", **kw):
    return rules.evaluate(
        Decision(**decision),
        portfolio or _portfolio(market=market),
        limits=LIMITS,
        stance=st,
        market=market,
        price=Decimal(price),
        today=D,
        **kw,
    )


def test_hold_is_noop():
    v = _eval(ok_decision(action="HOLD", target_weight=0.0))
    assert v.approved is False and v.adjusted_qty == 0 and "HOLD" in v.reasons[0]
    assert v.account_id == "paper-main"


def test_qty_from_weight_and_position_cap():
    # 0.1 × 1천만 / 7만 = 14.28 → 14주
    v = _eval(ok_decision(target_weight=0.1))
    assert v.approved and v.adjusted_qty == 14 and v.reasons == []
    # target_weight 는 주문 뒤 총 비중: 이미 20% 보유 + 목표 0.25 → 5% 만 추가 (50만/7만 = 7주)
    p = _portfolio(positions={"005930": _pos("005930", qty=20, price="100000")})
    v = _eval(ok_decision(action="ADD", target_weight=0.25), p)
    assert v.approved and v.adjusted_qty == 7
    # 이미 목표 비중 이상 보유면 ADD 는 0주 거부 (목표 비중을 다시 통째로 사지 않는다)
    v = _eval(ok_decision(action="ADD", target_weight=0.1), p)
    assert v.approved is False and v.adjusted_qty == 0
    assert any("이미 목표 비중" in r for r in v.reasons)


def test_limit_order_qty_uses_limit_price():
    """지정가가 현재가보다 높으면 수량은 지정가 기준이어야 주문금액이 한도를 넘지 않는다."""
    # 0.1 × 1천만 = 100만. 현재가 7만이면 14주지만 지정가 7.35만 기준 13주 (13×73500 = 955,500)
    v = _eval(ok_decision(target_weight=0.1, order_type="LIMIT", limit_price=73500.0))
    assert v.approved and v.adjusted_qty == 13 and v.price == Decimal("73500")


def test_buy_with_stop_loss_at_or_above_price_rejected():
    """손절선이 현재가 이상이면 모니터 첫 점검에서 즉시 매도되므로 매수를 거부한다."""
    v = _eval(ok_decision(stop_loss=72000.0), price="70000")
    assert v.approved is False and any("손절선" in r for r in v.reasons)
    v = _eval(ok_decision(stop_loss=65000.0), price="70000")
    assert v.approved


def test_max_positions_blocks_new():
    p = _portfolio(positions={f"00000{i}": _pos(f"00000{i}") for i in range(5)})
    v = _eval(ok_decision(), p)
    assert v.approved is False and any("종목 수" in r for r in v.reasons)
    # 보유 종목 ADD 는 종목 수 제한에 걸리지 않는다
    p.positions["005930"] = _pos("005930", qty=1)
    del p.positions["000000"]
    v = _eval(ok_decision(action="ADD"), p)
    assert v.approved


def test_daily_loss_stop_blocks_buy_not_sell():
    p = _portfolio(today_pnl_pct=-0.06, positions={"005930": _pos("005930", qty=10)})
    v = _eval(ok_decision(), p)
    assert v.approved is False and any("일일 손실" in r for r in v.reasons)
    p.held_since = {"005930": date(2026, 9, 1)}
    v = _eval(ok_decision(action="SELL", target_weight=0.0), p)
    assert v.approved and v.side == "SELL" and v.adjusted_qty == 10


def test_duplicate_buy_same_day_rejected():
    p = _portfolio(today_buy_tickers={"005930"})
    v = _eval(ok_decision(), p)
    assert v.approved is False and any("당일" in r and "매수" in r for r in v.reasons)


def test_cash_available_minus_today_orders():
    # 가능금액 100만 − 당일 누적 50만 = 50만 → 7주로 축소
    p = _portfolio(cash_available=Decimal("1000000"), today_order_amount=Decimal("500000"))
    v = _eval(ok_decision(target_weight=0.1), p)
    assert v.approved and v.adjusted_qty == 7 and any("가능금액" in r for r in v.reasons)
    p = _portfolio(cash_available=Decimal("50000"))
    v = _eval(ok_decision(target_weight=0.1), p)
    assert v.approved is False and v.adjusted_qty == 0


def test_min_hold_days_except_stop_loss():
    p = _portfolio(
        positions={"005930": _pos("005930", qty=10)}, held_since={"005930": date(2026, 10, 8)}
    )
    v = _eval(ok_decision(action="SELL", target_weight=0.0), p)
    assert v.approved is False and any("보유" in r for r in v.reasons)
    v = _eval(ok_decision(action="SELL", target_weight=0.0), p, is_stop_loss=True)
    assert v.approved and v.adjusted_qty == 10
    # 3거래일 이상이면 통과 (10/6 월 매수 → 10/9 목)
    p.held_since = {"005930": date(2026, 10, 6)}
    v = _eval(ok_decision(action="TRIM", target_weight=0.05), p)
    assert v.approved and v.side == "SELL" and 0 < v.adjusted_qty < 10


def test_weekly_trade_cap():
    p = _portfolio(trades_this_week={"005930": 2})
    v = _eval(ok_decision(), p)
    assert v.approved is False and any("주 2회" in r for r in v.reasons)


def test_expected_return_below_cost_rejected():
    # 왕복 30bp × 2 = 0.6% 미만 기대수익 거부: 70000 → take_profit 70300 (0.43%)
    v = _eval(ok_decision(take_profit=70300.0))
    assert v.approved is False and any("기대수익" in r for r in v.reasons)
    # 검토 L9: 익절가가 없으면 이제 통과하지 않는다(손절·익절 둘 다 필요)
    v = _eval(ok_decision(take_profit=None, limit_price=None))
    assert not v.approved and any("손절가와 익절가가 모두 필요" in r for r in v.reasons)


def test_low_confidence_halves():
    v = _eval(ok_decision(confidence=0.4))
    assert v.approved and v.adjusted_qty == 7 and any("confidence" in r for r in v.reasons)


def test_us_market_order_forced_limit():
    v = _eval(
        ok_decision(
            ticker="AAPL", order_type="MARKET", limit_price=None, take_profit=240.0, stop_loss=180.0
        ),
        price="200",
        market="US",
    )
    assert v.approved and v.order_type == "LIMIT" and v.price == Decimal("200")
    assert any("지정가" in r for r in v.reasons)
    v = _eval(ok_decision(order_type="MARKET", limit_price=None, take_profit=None))
    assert v.order_type == "MARKET" and v.price is None


def test_conservative_stance_limits():
    assert stance.decide(Decimal("-1")) == "conservative"
    assert stance.decide(Decimal("0")) == "normal"
    lim = stance.limits_for("conservative", LIMITS)
    assert (lim.max_position_pct, lim.max_positions) == (0.15, 2)
    assert stance.limits_for("normal", LIMITS) == LIMITS
    # conservative: 목표 0.2 → 0.15 로 잘림 → 150만/7만 = 21주
    v = _eval(ok_decision(target_weight=0.2), st="conservative")
    assert v.adjusted_qty == 21


def test_costs():
    assert costs.round_trip_bp("KR") == 30 and costs.round_trip_bp("US") == 50
    er = costs.expected_return(Decision(**ok_decision()), Decimal("70000"))
    assert abs(er - (80000 / 70000 - 1)) < 1e-9
    assert (
        costs.expected_return(
            Decision(**ok_decision(take_profit=None, limit_price=None)), Decimal("70000")
        )
        is None
    )


def test_min_hold_skips_holidays():
    # 10/6(월) 매수 → 10/9(목): 평일 3거래일이지만 10/8 이 휴장이면 2거래일
    p = _portfolio(
        positions={"005930": _pos("005930", qty=10)}, held_since={"005930": date(2026, 10, 6)}
    )
    v = _eval(ok_decision(action="SELL", target_weight=0.0), p)
    assert v.approved
    v = _eval(ok_decision(action="SELL", target_weight=0.0), p, holidays={date(2026, 10, 8)})
    assert v.approved is False and any("보유" in r for r in v.reasons)
    assert (
        rules._trading_days_between(date(2026, 10, 6), date(2026, 10, 9), {date(2026, 10, 8)}) == 2
    )


def test_min_cash_pct_reserves_cash():
    """risk.min_cash_pct 는 가능금액에서 NAV × 비율을 유보한다 (게이트가 무시하던 설정 필드)."""
    lim = RiskLimits(min_cash_pct=0.5)
    # 가능 1천만 − 유보 500만 = 500만 ≥ 목표 100만 → 수량 불변(14주), 사유 없음
    v = rules.evaluate(
        Decision(**ok_decision(target_weight=0.1, limit_price=None, order_type="MARKET")),
        _portfolio(),
        limits=lim,
        stance="normal",
        market="KR",
        price=Decimal("70000"),
        today=D,
    )
    assert v.approved and v.adjusted_qty == 14 and not any("유보" in r for r in v.reasons)
    # 현금 200만, 하한 10%(100만) → 가능 100만, 목표 250만 → 100만/7만 = 14주 + 사유
    v = rules.evaluate(
        Decision(**ok_decision(target_weight=0.25, limit_price=None, order_type="MARKET")),
        _portfolio(cash_available=Decimal("2000000")),
        limits=RiskLimits(min_cash_pct=0.1),
        stance="normal",
        market="KR",
        price=Decimal("70000"),
        today=D,
    )
    assert v.approved and v.adjusted_qty == 14 and any("유보" in r for r in v.reasons)


def test_reserve_binding_emits_single_reason():
    """유보가 묶으면 축소 사유는 하나(유보 포함)만 남긴다 (같은 축소를 두 번 적지 않음)."""
    v = rules.evaluate(
        Decision(**ok_decision(target_weight=0.098, limit_price=None, order_type="MARKET")),
        _portfolio(cash_available=Decimal("1000000")),
        limits=RiskLimits(min_cash_pct=0.05),
        stance="normal",
        market="KR",
        price=Decimal("70000"),
        today=D,
    )
    cuts = [r for r in v.reasons if "축소" in r]
    assert len(cuts) == 1 and "유보" in cuts[0]


def test_limit_price_is_quantized_to_tick():
    """LLM 지정가는 호가단위로 맞춰야 KIS 가 거부하지 않는다 (KR 5만~20만원 100원, US 0.01)."""
    v = _eval(ok_decision(limit_price=70350.0))
    assert v.approved and v.price == Decimal("70400")
    v = _eval(ok_decision(limit_price=20030.0, target_weight=0.05), price="20000")
    assert v.price == Decimal("20050")
    us = _eval(ok_decision(ticker="AAPL", limit_price=253.125), price="250", market="US")
    assert us.price == Decimal("253.13")


def test_tick_table():
    from trader.risk.ticks import quantize_price

    assert quantize_price("KR", Decimal("1999.6")) == Decimal("2000")
    assert quantize_price("KR", Decimal("4997")) == Decimal("4995")
    assert quantize_price("KR", Decimal("19995")) == Decimal("20000")
    assert quantize_price("KR", Decimal("49975")) == Decimal("50000")
    assert quantize_price("KR", Decimal("199950")) == Decimal("200000")
    assert quantize_price("KR", Decimal("499800")) == Decimal("500000")
    assert quantize_price("KR", Decimal("1234567")) == Decimal("1235000")
    assert quantize_price("US", Decimal("0.12345")) == Decimal("0.1235")
    assert quantize_price("US", Decimal("10.005")) == Decimal("10.01")


# --- Phase 17 H: 국면·섹터 상한·헤지 면제 ------------------------------------------------


def test_regime_neutral_tightens_limits_and_bear_blocks_buy():
    lim = stance.limits_for_regime("neutral", LIMITS)
    assert (lim.max_position_pct, lim.max_positions) == (0.15, 3)
    assert stance.limits_for_regime("bull", LIMITS) == LIMITS
    v = _eval(ok_decision(target_weight=0.2), regime="neutral")
    assert v.adjusted_qty == 21 and any("neutral" in r for r in v.reasons)
    v = _eval(ok_decision(), regime="bear")
    assert not v.approved and any("bear" in r for r in v.reasons)
    # bear 에서도 매도·손절은 통과
    p = _portfolio(
        positions={"005930": _pos("005930", 10, "70000")},
        held_since={"005930": D - timedelta(days=30)},
    )
    v = _eval(ok_decision(action="SELL", target_weight=0.0), p, regime="bear")
    assert v.approved and v.side == "SELL"


def test_sector_cap_rejects_when_exceeded_and_skips_hedge_tickers():
    sectors = {"005930": "반도체", "000660": "반도체", "114800": None, "005380": "자동차"}
    p = _portfolio(
        positions={
            "000660": _pos("000660", 35, "100000"),  # 350만 = 35%
            "114800": _pos("114800", 100, "5000"),  # 헤지 50만 — 계산 제외
        },
        sector_of=sectors.get,
        hedge_tickers=frozenset({"114800"}),
    )
    # 005930 10% 추가 → 반도체 45% > 40% 거부
    v = _eval(ok_decision(target_weight=0.10), p)
    assert not v.approved and any("섹터 '반도체'" in r for r in v.reasons)
    # 다른 섹터는 통과하고 종목 수도 헤지를 빼고 센다 (일반 1종목)
    v = _eval(
        ok_decision(ticker="005380", target_weight=0.10, limit_price=70000.0, stop_loss=65000.0), p
    )
    assert v.approved
    assert len(p.equity_positions()) == 1
    # 비중이 상한 안이면 같은 섹터도 통과 (반도체 35% + 4% = 39%)
    v = _eval(ok_decision(target_weight=0.04, take_profit=80000.0), p)
    assert v.approved


def test_hedge_order_exempts_rules_and_uses_hedge_cap():
    p = _portfolio(
        positions={f"T{i}": _pos(f"T{i}", 1, "1000") for i in range(5)},  # 종목 수 상한 도달
        today_buy_tickers={"114800"},
        trades_this_week={"114800": 5},
        today_pnl_pct=-0.10,
        sector_of=lambda t: "기타",
    )
    dec = ok_decision(
        ticker="114800", target_weight=0.20, order_type="MARKET", limit_price=None,
        stop_loss=None, take_profit=None, confidence=1.0,
    )  # fmt: skip
    v = _eval(dec, p, price="5000", regime="bear", is_hedge=True, hedge_max_weight=0.20)
    assert v.approved and v.adjusted_qty == 400  # 1000만 × 20% / 5000
    # 일반 주문이면 같은 조건에서 거부 사유가 쌓인다
    v2 = _eval(dec, p, price="5000", regime="bear")
    assert not v2.approved and len(v2.reasons) >= 3
    # 헤지 비중 상한이 25% 기본 대신 쓰인다
    v3 = _eval(
        {**dec, "target_weight": 0.25}, p, price="5000", is_hedge=True, hedge_max_weight=0.10
    )
    assert v3.adjusted_qty == 200
    # 헤지 청산(SELL)은 최소 보유·주 2회를 면제
    p2 = _portfolio(
        positions={"114800": _pos("114800", 100, "5000")},
        held_since={"114800": D},
        trades_this_week={"114800": 5},
    )
    v4 = _eval({**dec, "action": "SELL", "target_weight": 0.0}, p2, price="5000", is_hedge=True)
    assert v4.approved and v4.adjusted_qty == 100


def test_sell_uses_sellable_qty_not_holding_qty():
    """매도가능수량(ord_psbl_qty)이 보유량보다 적으면(미체결 매도 등) 그만큼만 판다."""
    from decimal import Decimal

    from trader.agents.schemas import Decision
    from trader.broker.models import Position
    from trader.config import RiskLimits
    from trader.risk import rules

    held = Position(
        ticker="005930",
        market="KR",
        qty=10,
        avg_price=Decimal("70000"),
        current_price=Decimal("63000"),
        eval_amount=Decimal("630000"),
        pnl_pct=-0.1,
        sellable_qty=6,
    )
    pf = rules.Portfolio(
        account_id="a",
        market="KR",
        nav=Decimal("10000000"),
        cash_available=Decimal("5000000"),
        positions={"005930": held},
        today_pnl_pct=0.0,
        today_buy_tickers=set(),
        today_order_amount=Decimal(0),
        held_since={},
        trades_this_week=0,
    )
    dec = Decision(
        ticker="005930",
        action="SELL",
        target_weight=0.0,
        order_type="MARKET",
        limit_price=None,
        stop_loss=None,
        take_profit=None,
        horizon_days=1,
        confidence=1.0,
        rationale="x",
        invalidation_conditions=[],
    )
    v = rules.evaluate(
        dec,
        pf,
        limits=RiskLimits(),
        stance="normal",
        market="KR",
        price=Decimal("63000"),
        today=__import__("datetime").date(2026, 10, 9),
        is_stop_loss=True,
    )
    assert v.adjusted_qty == 6 and any("매도가능수량 6" in r for r in v.reasons)


def test_agent_tilt_shifts_regime_limits_one_step_and_opens_bear_conditionally():
    """Phase 22: 전략가 기울기 — neutral+RISK_ON 20%·4, neutral+RISK_OFF 10%·2, bull+RISK_OFF 는
    neutral 한도, bear+RISK_ON 은 10%·2 로 조건부 허용. 헤지·손절은 기울기와 무관."""
    f = stance.limits_for_regime
    assert (f("neutral", LIMITS, 1).max_position_pct, f("neutral", LIMITS, 1).max_positions) == (
        0.20,
        4,
    )
    assert (f("neutral", LIMITS, -1).max_position_pct, f("neutral", LIMITS, -1).max_positions) == (
        0.10,
        2,
    )
    assert f("bull", LIMITS, -1) == f("neutral", LIMITS) and f("bull", LIMITS, 1) == LIMITS
    assert (f("bear", LIMITS, 1).max_position_pct, f("bear", LIMITS, 1).max_positions) == (0.10, 2)
    assert f("bear", LIMITS, -1) == LIMITS
    # 설정 한도보다 넓어지지 않는다
    tight = LIMITS.model_copy(update={"max_position_pct": 0.12, "max_positions": 3})
    assert (f("neutral", tight, 1).max_position_pct, f("neutral", tight, 1).max_positions) == (
        0.12,
        3,
    )
    # tilt_of: 확신 기준·stance
    assert stance.tilt_of(None, min_confidence=0.7) == 0
    assert stance.tilt_of({"stance": "RISK_ON", "confidence": 0.8}, min_confidence=0.7) == 1
    assert stance.tilt_of({"stance": "RISK_ON", "confidence": 0.6}, min_confidence=0.7) == 0
    assert stance.tilt_of({"stance": "RISK_OFF", "confidence": 0.9}, min_confidence=0.7) == -1
    assert stance.tilt_of({"stance": "NEUTRAL", "confidence": 0.9}, min_confidence=0.7) == 0
    # 게이트: bear + RISK_ON → 0.2 요청이 10% 한도로 잘려 14주(0.10 × 1천만 / 7만)
    v = _eval(ok_decision(target_weight=0.2), regime="bear", agent_tilt=1)
    assert v.approved and v.adjusted_qty == 14
    assert any("조건부 허용" in r for r in v.reasons) and any("10%·2종목" in r for r in v.reasons)
    # bear + RISK_OFF/없음 → 여전히 거부
    assert not _eval(ok_decision(), regime="bear", agent_tilt=-1).approved
    # neutral + RISK_ON → 20% 한도(0.2 요청 그대로 28주)
    v = _eval(ok_decision(target_weight=0.2), regime="neutral", agent_tilt=1)
    assert v.adjusted_qty == 28 and any("전략가 RISK_ON" in r for r in v.reasons)
    # bull + RISK_OFF → neutral 한도 15%
    v = _eval(ok_decision(target_weight=0.2), regime="bull", agent_tilt=-1)
    assert v.adjusted_qty == 21 and any("전략가 RISK_OFF" in r for r in v.reasons)
    # 손절은 기울기 무관
    p = _portfolio(
        positions={"005930": _pos("005930", 10, "70000")},
        held_since={"005930": D - timedelta(days=1)},
    )
    v = _eval(
        ok_decision(action="SELL", target_weight=0.0),
        p,
        regime="bear",
        agent_tilt=-1,
        is_stop_loss=True,
    )
    assert v.approved


# --- Phase 25 단계 4: 테마 매매 안전장치 ---------------------------------------------------


def test_chase_limit_rejects_buy_at_10pct():
    v = _eval(ok_decision(), change_pct=0.10)
    assert not v.approved and any("+10%" in r and "신규 매수 금지" in r for r in v.reasons)
    assert _eval(ok_decision(), change_pct=0.099).approved
    v = _eval(
        ok_decision(action="ADD"),
        _portfolio(positions={"005930": _pos("005930", 1, "70000")}),
        change_pct=0.15,
    )
    assert not v.approved and any("신규 매수 금지" in r for r in v.reasons)


def test_change_pct_none_skips_rule():
    assert _eval(ok_decision(), change_pct=None).approved


def test_chase_limit_ignores_stop_loss_hedge_and_sell():
    held = _portfolio(
        positions={"005930": _pos("005930", 10, "70000")},
        held_since={"005930": D - timedelta(days=10)},
    )
    v = _eval(
        ok_decision(action="SELL", target_weight=0.0), held, change_pct=0.2, is_stop_loss=True
    )
    assert v.approved
    v = _eval(ok_decision(), change_pct=0.2, is_hedge=True, hedge_max_weight=0.1)
    assert not any("신규 매수 금지" in r for r in v.reasons)


def test_theme_cap_20pct():
    themes = {"005930": ["섹터:반도체"], "000660": ["섹터:반도체"]}
    pf = _portfolio(positions={"000660": _pos("000660", 15, "100000")}, theme_of=themes.get)  # 15%
    v = _eval(ok_decision(target_weight=0.1), pf)  # +10% → 25% > 20%
    assert not v.approved and any("테마" in r and "20%" in r for r in v.reasons)
    v = _eval(ok_decision(target_weight=0.04), pf)  # +4% → 19%
    assert v.approved


def test_theme_ticker_requires_short_horizon_and_take_profit():
    pf = _portfolio(theme_of={"005930": ["kw:로봇"]}.get)
    v = _eval(ok_decision(horizon_days=20), pf)
    assert not v.approved and any("보유 10일 이하" in r for r in v.reasons)
    v = _eval(ok_decision(take_profit=None), pf)
    assert not v.approved and any("익절가" in r for r in v.reasons)
    assert _eval(ok_decision(horizon_days=10), pf).approved
    # 테마 종목이 아니면 긴 보유는 통과(익절가 필수는 이제 모든 BUY 규칙 — 검토 L9)
    assert _eval(ok_decision(horizon_days=30)).approved
    v = _eval(ok_decision(horizon_days=30, take_profit=None))
    assert not any("보유 10일 이하" in r for r in v.reasons)


def test_theme_cap_checks_every_overlapping_theme():
    """종목이 섹터·키워드 테마에 함께 속하면 각 테마의 합계를 따로 본다(PR #26 리뷰)."""
    themes = {"000660": ["섹터:반도체", "kw:로봇"], "005930": ["kw:로봇"]}
    pf = _portfolio(positions={"000660": _pos("000660", 15, "100000")}, theme_of=themes.get)
    v = _eval(ok_decision(target_weight=0.1), pf)  # 로봇 15% + 10% = 25% > 20%
    assert not v.approved and any("kw:로봇" in r and "초과" in r for r in v.reasons)


# --- 검토 L9: 손절·익절 필수, 확신 가중 기대값, 1회 손실 한도 -----------------------------


def test_buy_requires_both_stop_and_take_profit():
    v = _eval(ok_decision(stop_loss=None))
    assert not v.approved and any("모두 필요" in r for r in v.reasons)


def test_negative_expected_value_rejected_even_if_target_far_enough():
    # 현재 70000, 익절 +2%(71400)·손절 −20%(56000), 확신 0.6 → EV = 0.6×2% − 0.4×20% < 0
    v = _eval(ok_decision(take_profit=71400.0, stop_loss=56000.0, limit_price=None))
    assert not v.approved and any("기대값" in r for r in v.reasons)


def test_risk_per_trade_caps_quantity_by_stop_distance():
    # NAV 1000만 × 2% = 20만 원 위험, 손절까지 70000−69000 = 1000원 → 최대 200주(1400만 아님 →
    # 목표 비중 25%=250만/70000=35주가 더 작으니 35주). 손절을 가깝게 두면 비중이 그대로,
    # 손절을 멀리(−20%) 두면 20만/14000=14주로 줄어든다
    near = _eval(
        ok_decision(target_weight=0.25, stop_loss=69000.0, take_profit=80000.0, limit_price=None)
    )
    far = _eval(
        ok_decision(target_weight=0.25, stop_loss=56000.0, take_profit=95000.0, limit_price=None)
    )
    assert near.approved and near.adjusted_qty == 35
    assert far.approved and far.adjusted_qty == 14
    assert any("1회 손실 한도" in r for r in far.reasons)
