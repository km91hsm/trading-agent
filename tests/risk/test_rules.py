from datetime import date
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
    # 이미 20% 보유 + 목표 0.1 → 종목당 25% 상한까지 5% 만 추가 (50만/7만 = 7주)
    p = _portfolio(positions={"005930": _pos("005930", qty=20, price="100000")})
    v = _eval(ok_decision(action="ADD", target_weight=0.1), p)
    assert v.approved and v.adjusted_qty == 7 and any("종목당" in r for r in v.reasons)


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
    v = _eval(ok_decision(take_profit=None, limit_price=None))  # 기대수익 미상이면 통과
    assert v.approved


def test_low_confidence_halves():
    v = _eval(ok_decision(confidence=0.4))
    assert v.approved and v.adjusted_qty == 7 and any("confidence" in r for r in v.reasons)


def test_us_market_order_forced_limit():
    v = _eval(
        ok_decision(ticker="AAPL", order_type="MARKET", limit_price=None, take_profit=None),
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
