"""리스크 규칙 (스펙 §6.1, 순수 함수). 계좌 하나의 Decision 을 판정해 RiskVerdict 를 돌려준다.

거부 사유는 전부 모아 돌려주며(첫 사유에서 멈추지 않음), 수량 조정 사유도 같은 목록에 남긴다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_FLOOR, Decimal

from pydantic import BaseModel, ConfigDict

from trader.agents.schemas import Decision
from trader.broker.models import Position
from trader.config import RiskLimits
from trader.risk import costs
from trader.risk.stance import Stance, limits_for, limits_for_regime
from trader.risk.ticks import quantize_price

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
    # Phase 17 H: 섹터 상한용 종목→섹터(없으면 섹터 규칙 생략), 헤지 ETF(종목 수·섹터 계산에서 제외)
    sector_of: Callable[[str], str | None] | None = None
    hedge_tickers: frozenset[str] = frozenset()
    # Phase 25: 오늘 상위 테마 종목→그 종목이 속한 상위 테마들(scan/theme.theme_of_map, 섹터·키워드
    # 테마가 겹칠 수 있다). 없으면 테마 규칙 생략
    theme_of: Callable[[str], list[str] | None] | None = None

    def equity_positions(self) -> dict[str, Position]:
        """헤지 ETF 를 뺀 일반 보유 종목."""
        return {t: p for t, p in self.positions.items() if t not in self.hedge_tickers}


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


def _theme_rejections(
    decision: Decision,
    portfolio: Portfolio,
    lim: RiskLimits,
    *,
    change_pct: float | None,
    current_value: Decimal,
    order_amount: Decimal,
) -> list[str]:
    """테마 매매 안전장치(Phase 25). 일반 BUY/ADD 에서만 부른다. 거부 사유 목록."""
    out: list[str] = []
    if change_pct is not None and change_pct >= lim.chase_limit_pct:
        out.append(
            f"당일 등락률 {change_pct:+.1%} ≥ +{lim.chase_limit_pct:.0%}: "
            "급등 추격 — 신규 매수 금지"
        )
    theme_of = portfolio.theme_of
    themes = list(theme_of(decision.ticker) or []) if theme_of is not None else []
    if not themes:
        return out
    if decision.horizon_days > lim.theme_max_horizon_days or decision.take_profit is None:
        out.append(
            f"테마 종목('{themes[0]}')은 보유 {lim.theme_max_horizon_days}일 이하·익절가 필수 "
            f"(결정: {decision.horizon_days}일, 익절가 {decision.take_profit or '없음'})"
        )
    nav = portfolio.nav
    if nav > 0:
        # 종목이 속한 상위 테마마다 따로 본다(섹터 테마와 키워드 테마가 겹치면 둘 다 상한 대상)
        for theme in themes:
            others = sum(
                (
                    p.eval_amount
                    for t, p in portfolio.equity_positions().items()
                    if t != decision.ticker and theme in (theme_of(t) or [])  # type: ignore[misc]
                ),
                Decimal(0),
            )
            after = (others + current_value + order_amount) / nav
            if after > Decimal(str(lim.max_theme_pct)):
                out.append(
                    f"테마 '{theme}' 비중이 주문 뒤 {float(after):.0%} 로 상한 "
                    f"{lim.max_theme_pct:.0%} 초과"
                )
    return out


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
    regime: str = "bull",
    is_hedge: bool = False,
    hedge_max_weight: float | None = None,
    agent_tilt: int = 0,
    is_scalp: bool = False,
    change_pct: float | None = None,
) -> RiskVerdict:
    """regime(bull|neutral|bear)·is_hedge 는 Phase 17 H: neutral 은 신규 매수 한도 축소, bear 는
    일반 BUY/ADD 거부(SELL·TRIM·손절·헤지만 통과). 헤지 주문은 비용·보유기간·주 2회·중복·종목 수·
    섹터·일일손실 규칙을 면제하고 비중 상한만 hedge_max_weight 로 바꾼다.
    agent_tilt(Phase 22)는 전략가 제안의 기울기(+1 RISK_ON / −1 RISK_OFF): 국면 한도를 한 칸
    옮기고, bear 에서 +1 이면 신규 매수를 10%·2종목 한도로 조건부 허용한다.
    is_scalp(Phase 23)는 분 단위 단타: 최소 보유·주 2회·기대수익 규칙을 면제하고(당일 청산 전제)
    비중·현금·중복·종목 수·섹터·일일손실·bear 거부는 그대로 본다.
    change_pct·theme_of(Phase 25 테마 매매 안전장치, 일반 BUY/ADD 만 — 헤지·손절·단타 면제):
    당일 등락률 ≥ chase_limit_pct 면 신규 매수 금지, 오늘 상위 테마 종목이면 같은 테마 합계
    ≤ max_theme_pct 와 보유 ≤ theme_max_horizon_days·익절가 필수."""
    acct = portfolio.account_id
    if decision.action == "HOLD":
        return RiskVerdict(
            account_id=acct, approved=False, adjusted_qty=0, reasons=["HOLD: 주문 없음"]
        )

    tilt = 0 if is_hedge or is_stop_loss or is_scalp else int(agent_tilt)
    lim = limits_for_regime(regime, limits_for(stance, limits), tilt)
    if is_hedge and hedge_max_weight is not None:
        lim = lim.model_copy(update={"max_position_pct": hedge_max_weight})
    side = "BUY" if decision.action in ("BUY", "ADD") else "SELL"
    reasons: list[str] = []
    rejected: list[str] = []
    if side == "BUY" and not is_hedge and (regime != "bull" or tilt != 0):
        who = {1: "전략가 RISK_ON", -1: "전략가 RISK_OFF"}.get(tilt, "")
        base = f"국면 {regime}" + (f" + {who}" if who else "")
        reasons.append(
            f"{base}: 신규 매수 한도(종목당 {lim.max_position_pct:.0%}·{lim.max_positions}종목)"
        )
    ticker = decision.ticker
    held = portfolio.positions.get(ticker)
    nav = portfolio.nav

    # --- 주문 유형·가격 (US 는 지정가 강제, 모의 서버 제약) -------------------------
    order_type = decision.order_type
    if market == "US" and order_type == "MARKET":
        order_type = "LIMIT"
        reasons.append("US 는 지정가만 허용: 시장가를 현재가 지정가로 전환")
    if order_type == "LIMIT":
        raw_price = (
            Decimal(str(decision.limit_price)) if decision.limit_price is not None else price
        )
        # 호가단위 밖 지정가는 KIS 가 거부해 그날 결정이 통째로 날아간다 → 가장 가까운 단위로
        order_price: Decimal | None = quantize_price(market, raw_price)
        if order_price != raw_price:
            reasons.append(f"지정가 {raw_price} 를 호가단위에 맞춰 {order_price} 로 조정")
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
        reserve = Decimal(str(lim.min_cash_pct)) * nav if lim.min_cash_pct > 0 else Decimal(0)
        if buy_value > available - reserve:
            # 한 번의 축소에 사유는 하나 (유보가 있으면 문구에 함께 적는다)
            held_back = (
                f", 현금 보유 하한 {lim.min_cash_pct:.0%} (NAV 대비) 유보" if reserve > 0 else ""
            )
            reasons.append(f"가능금액(당일 누적 주문 차감{held_back}) 초과로 수량 축소")
            buy_value = max(available - reserve, Decimal(0))
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
        else:
            sellable = getattr(held, "sell_qty", held.qty)  # 매도가능수량(미체결 매도분 제외)
            if sellable < held.qty:
                reasons.append(f"매도가능수량 {sellable} (보유 {held.qty})")
            if decision.action == "SELL":
                qty = sellable
            else:  # TRIM: 목표 비중까지만 줄인다
                keep_value = Decimal(str(decision.target_weight)) * nav
                qty = min(
                    sellable, _floor_qty(max(held.eval_amount - keep_value, Decimal(0)), price)
                )

    # --- 거부 규칙 (전부 수집) ---------------------------------------------------------
    if side == "BUY" and is_hedge:
        if qty <= 0 and not rejected:
            rejected.append("가능금액 부족으로 주문 수량 0")
    elif side == "BUY":
        if regime == "bear" and tilt <= 0:
            rejected.append("하락 국면(bear): 신규 매수·추가 매수 금지")
        elif regime == "bear":
            reasons.append(
                "하락 국면(bear)이지만 전략가 RISK_ON(확신 기준 이상): 축소 한도로 조건부 허용"
            )
        equities = portfolio.equity_positions()
        if held is None and len(equities) >= lim.max_positions:
            rejected.append(f"보유 종목 수 {len(equities)} 이 상한 {lim.max_positions} 에 도달")
        if portfolio.sector_of is not None and nav > 0:
            sector = portfolio.sector_of(ticker) or "기타"
            others = sum(
                (
                    p.eval_amount
                    for t, p in equities.items()
                    if t != ticker and (portfolio.sector_of(t) or "기타") == sector
                ),
                Decimal(0),
            )
            # 반감·수량 절사 뒤의 실제 주문금액으로 본다(buy_value 는 반감 전 가능금액)
            order_amount = Decimal(qty) * (order_price if order_price is not None else price)
            after = (others + current_value + order_amount) / nav
            if after > Decimal(str(lim.max_sector_pct)):
                rejected.append(
                    f"섹터 '{sector}' 비중이 주문 뒤 {float(after):.0%} 로 상한 "
                    f"{lim.max_sector_pct:.0%} 초과"
                )
        if not is_scalp:
            rejected += _theme_rejections(
                decision,
                portfolio,
                lim,
                change_pct=change_pct,
                current_value=current_value,
                order_amount=Decimal(qty) * (order_price if order_price is not None else price),
            )
        if portfolio.today_pnl_pct <= lim.daily_loss_stop_pct:
            rejected.append(
                f"일일 손실 {portfolio.today_pnl_pct:.2%} 이 한도 {lim.daily_loss_stop_pct:.0%} "
                "초과: 신규 매수 거부"
            )
        if ticker in portfolio.today_buy_tickers:
            rejected.append("당일 이미 매수 주문이 있는 종목: 중복 매수 거부")
        er = None if is_scalp else costs.expected_return(decision, price)
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
        if since is not None and not is_stop_loss and not is_hedge and not is_scalp:
            days = _trading_days_between(since, today, holidays)
            if days < MIN_HOLD_TRADING_DAYS:
                rejected.append(
                    f"최소 보유 {MIN_HOLD_TRADING_DAYS}거래일 미충족 (보유 {days}거래일)"
                )
        if qty <= 0 and not rejected:
            rejected.append("매도 수량 0")
    if (
        not is_stop_loss
        and not is_hedge
        and not is_scalp
        and portfolio.trades_this_week.get(ticker, 0) >= MAX_TRADES_PER_WEEK
    ):
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
