"""리스크 규칙 (스펙 §6.1, 순수 함수). 계좌 하나의 Decision 을 판정해 RiskVerdict 를 돌려준다.

거부 사유는 전부 모아 돌려주며(첫 사유에서 멈추지 않음), 수량 조정 사유도 같은 목록에 남긴다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_FLOOR, Decimal

from pydantic import BaseModel, ConfigDict

from trader.agents.schemas import Decision
from trader.broker.models import Position
from trader.config import RiskLimits
from trader.risk import costs
from trader.risk.stance import Stance, limits_for

MIN_HOLD_TRADING_DAYS = 3
MAX_TRADES_PER_WEEK = 2
LOW_CONFIDENCE = 0.5
COST_MULTIPLE = 2


@dataclass
class Portfolio:
    """계좌 포트폴리오 스냅샷 + 원장 집계 (executor 가 조립)."""

    account_id: str
    market: str
    nav: Decimal
    cash_available: Decimal
    positions: dict[str, Position] = field(default_factory=dict)
    today_pnl_pct: float = 0.0
    today_buy_tickers: set[str] = field(default_factory=set)
    today_order_amount: Decimal = Decimal(0)
    held_since: dict[str, date] = field(default_factory=dict)
    trades_this_week: dict[str, int] = field(default_factory=dict)


class RiskVerdict(BaseModel):
    """체크포인트 `40_risk_check_<account>` 의 data."""

    model_config = ConfigDict(extra="forbid")

    account_id: str
    approved: bool
    adjusted_qty: int
    reasons: list[str]
    side: str | None = None  # BUY | SELL
    order_type: str | None = None  # LIMIT | MARKET
    price: Decimal | None = None
    # executor 가 채우는 실행 맥락 (체크포인트 저장용)
    mode: str | None = None
    note: str | None = None
    stance: str | None = None
    dry_run: bool | None = None


def _floor_qty(value: Decimal, price: Decimal) -> int:
    if price <= 0:
        return 0
    return int((value / price).to_integral_value(rounding=ROUND_FLOOR))


def _trading_days_between(start: date, end: date, holidays: set[date] | None = None) -> int:
    """start 다음 날부터 end 까지의 거래일 수 (주말·휴장일 제외)."""
    n, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5 and not (holidays and d in holidays):
            n += 1
    return n


def evaluate(
    decision: Decision,
    portfolio: Portfolio,
    *,
    limits: RiskLimits,
    stance: Stance,
    market: str,
    price: Decimal,
    today: date,
    is_stop_loss: bool = False,
    holidays: set[date] | None = None,
) -> RiskVerdict:
    acct = portfolio.account_id
    if decision.action == "HOLD":
        return RiskVerdict(
            account_id=acct, approved=False, adjusted_qty=0, reasons=["HOLD: 주문 없음"]
        )

    lim = limits_for(stance, limits)
    side = "BUY" if decision.action in ("BUY", "ADD") else "SELL"
    reasons: list[str] = []
    rejected: list[str] = []
    ticker = decision.ticker
    held = portfolio.positions.get(ticker)
    nav = portfolio.nav

    # --- 주문 유형·가격 (US 는 지정가 강제, 모의 서버 제약) -------------------------
    order_type = decision.order_type
    if market == "US" and order_type == "MARKET":
        order_type = "LIMIT"
        reasons.append("US 는 지정가만 허용: 시장가를 현재가 지정가로 전환")
    if order_type == "LIMIT":
        order_price: Decimal | None = (
            Decimal(str(decision.limit_price)) if decision.limit_price is not None else price
        )
    else:
        order_price = None

    # --- 수량 산출 -------------------------------------------------------------------
    if side == "BUY":
        weight = decision.target_weight
        if weight > lim.max_position_pct:
            reasons.append(
                f"목표 비중 {weight:.2f} 을 종목당 상한 {lim.max_position_pct:.2f} 로 조정"
            )
            weight = lim.max_position_pct
        # target_weight 는 주문 뒤의 총 비중이다 (ADD 도 TRIM 과 같은 의미). 매수금액은 그 증분.
        target_value = Decimal(str(weight)) * nav
        current_value = held.eval_amount if held else Decimal(0)
        buy_value = target_value - current_value
        if held is not None and buy_value <= 0:
            buy_value = Decimal(0)
            rejected.append(
                f"이미 목표 비중 이상 보유 (현재 {float(current_value / nav) if nav else 0:.2f} ≥ "
                f"목표 {weight:.2f})"
            )
        room = Decimal(str(lim.max_position_pct)) * nav - current_value
        if buy_value > room:
            buy_value = max(room, Decimal(0))
            reasons.append(f"종목당 상한 {lim.max_position_pct:.0%} 까지만 추가 매수")
        available = portfolio.cash_available - portfolio.today_order_amount
        if buy_value > available:
            buy_value = max(available, Decimal(0))
            reasons.append("가능금액(당일 누적 주문 차감) 초과로 수량 축소")
        # 수량은 실제 주문가 기준 (지정가가 현재가보다 높으면 현재가 기준 수량은 금액을 초과한다)
        qty = _floor_qty(buy_value, order_price if order_price is not None else price)
        if decision.stop_loss is not None and Decimal(str(decision.stop_loss)) >= price:
            rejected.append(
                f"손절선 {decision.stop_loss} 이 현재가 {price} 이상 (모니터가 즉시 손절 매도함)"
            )
        if decision.confidence < LOW_CONFIDENCE:
            qty //= 2
            reasons.append(f"confidence {decision.confidence:.2f} < {LOW_CONFIDENCE} 로 수량 절반")
    else:
        if held is None or held.qty <= 0:
            qty = 0
            rejected.append("보유하지 않은 종목은 매도할 수 없음")
        elif decision.action == "SELL":
            qty = held.qty
        else:  # TRIM: 목표 비중까지만 줄인다
            keep_value = Decimal(str(decision.target_weight)) * nav
            qty = min(held.qty, _floor_qty(max(held.eval_amount - keep_value, Decimal(0)), price))

    # --- 거부 규칙 (전부 수집) ---------------------------------------------------------
    if side == "BUY":
        if held is None and len(portfolio.positions) >= lim.max_positions:
            rejected.append(
                f"보유 종목 수 {len(portfolio.positions)} 이 상한 {lim.max_positions} 에 도달"
            )
        if portfolio.today_pnl_pct <= lim.daily_loss_stop_pct:
            rejected.append(
                f"일일 손실 {portfolio.today_pnl_pct:.2%} 이 한도 {lim.daily_loss_stop_pct:.0%} "
                "초과: 신규 매수 거부"
            )
        if ticker in portfolio.today_buy_tickers:
            rejected.append("당일 이미 매수 주문이 있는 종목: 중복 매수 거부")
        er = costs.expected_return(decision, price)
        if er is not None:
            threshold = costs.round_trip_bp(market) * COST_MULTIPLE / 10_000
            if er < threshold:
                rejected.append(
                    f"기대수익 {er:.2%} 이 왕복비용×{COST_MULTIPLE}({threshold:.2%}) 미만"
                )
        if qty <= 0 and not rejected:
            rejected.append("가능금액 부족으로 주문 수량 0")
    else:
        since = portfolio.held_since.get(ticker)
        if since is not None and not is_stop_loss:
            days = _trading_days_between(since, today, holidays)
            if days < MIN_HOLD_TRADING_DAYS:
                rejected.append(
                    f"최소 보유 {MIN_HOLD_TRADING_DAYS}거래일 미충족 (보유 {days}거래일)"
                )
        if qty <= 0 and not rejected:
            rejected.append("매도 수량 0")
    if not is_stop_loss and portfolio.trades_this_week.get(ticker, 0) >= MAX_TRADES_PER_WEEK:
        rejected.append(f"종목당 주 {MAX_TRADES_PER_WEEK}회 거래 한도 초과")

    approved = not rejected and qty > 0
    return RiskVerdict(
        account_id=acct,
        approved=approved,
        adjusted_qty=qty if approved else 0,
        reasons=reasons + rejected,
        side=side,
        order_type=order_type,
        price=order_price,
    )
