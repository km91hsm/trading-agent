"""게이트 테스트 공용 픽스처(규칙·사이징·실행)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

from trader.broker.fo_master import Contract
from trader.broker.models import FoCash, FoOrderable, FoQuote
from trader.config import FoCfg
from trader.fo.schemas import FoDecision, FoDecisionEnvelope
from trader.fo.types import FoLegRow, FoPortfolio, FoPositionRow, GateInput

ACCT = "paper-fo"
NOW = datetime(2026, 10, 27, 1, 30, tzinfo=UTC)  # 10:30 KST, 진입 창 안
TODAY = date(2026, 10, 27)
NAV = Decimal("100000000")  # 1억: 미니 1계약 델타명목 1,750만 ≈ 0.175배


def cfg(**over) -> FoCfg:
    base = {"enabled": True}
    base.update(over)
    return FoCfg(**base)


def cash(nav: Decimal | None = NAV, add_margin: Decimal = Decimal(0)) -> FoCash:
    return FoCash(
        deposit=nav or Decimal(0),
        orderable_cash=Decimal("50000000"),
        orderable_total=Decimal("50000000"),
        margin_total=Decimal("0"),
        add_margin=add_margin,
        nav=nav,
        realized_today=Decimal("0"),
        unrealized=Decimal("0"),
        requested_at=NOW,
        received_at=NOW,
    )


def quote(code="105W11", kind="FUTURES", price="350.5", **over) -> FoQuote:
    base = dict(
        code=code,
        kind=kind,
        price=Decimal(price),
        bid=Decimal(price) - Decimal("0.02"),
        ask=Decimal(price) + Decimal("0.02"),
        change_pct=0.0,
        volume=1,
        open_interest=1,
        basis=Decimal("0.5"),
        upper=None,
        lower=None,
        last_trade_date=date(2026, 11, 12),
        days_left=16,
        hist_vol=15.0,
        iv=None,
        delta=None,
        gamma=None,
        theta=None,
        vega=None,
        strike=None,
        underlying=Decimal("350"),
        underlying_asof=NOW,
        quote_asof=NOW,
        received_at=NOW,
    )
    base.update(over)
    return FoQuote(**base)


def option_quote(kind="CALL", code="205W11350", price="5.00", delta=0.5, iv=18.0, **over):
    return quote(
        code=code,
        kind=kind,
        price=price,
        delta=delta if kind == "CALL" else -abs(delta),
        iv=iv,
        strike=Decimal("350"),
        basis=None,
        **over,
    )


def contract(code="105W11", kind="FUTURES", strike=None) -> Contract:
    return Contract(
        code=code,
        std_code="KR4" + code + "000",
        name=code,
        kind=kind,
        weekly=False,
        strike=strike,
        expiry_ym="202611",
        underlying="K200",
        atm_flag=None,
        product_type="",
    )


def decision(**over) -> FoDecision:
    base = dict(
        intent="OPEN",
        direction="LONG",
        vehicle="FUTURES",
        target_leverage=1.0,
        order_type="LIMIT",
        entry_price=350.5,
        stop_pt=345.0,
        take_profit_pt=360.0,
        confidence=0.7,
        rationale="t",
        horizon_days=5,
    )
    base.update(over)
    return FoDecision(**base)


def position(**over) -> FoPositionRow:
    base = dict(
        id=1,
        account_id=ACCT,
        code="105W11",
        kind="FUTURES",
        contract_side="LONG",
        direction="LONG",
        vehicle="FUTURES",
        status="held",
        qty_target=2,
        qty_held=2,
        leverage_used=1.0,
        stop_pt=Decimal("345"),
        stop_fut=None,
        tp_pt=None,
        approved_mult=None,
        premium_stop=None,
        entry_premium=None,
        nav_at_open=NAV,
        realized_loss=Decimal(0),
        fees=Decimal(0),
        active_reduce_leg_id=None,
        horizon_until=None,
        last_trade_date="2026-11-12",
        decision_date="2026-10-27",
        round=0,
        version=3,
        opened_at=None,
        closed_at=None,
        note=None,
        created_at="t",
    )
    base.update(over)
    return FoPositionRow(**base)


def leg(**over) -> FoLegRow:
    base = dict(
        id=7,
        position_id=1,
        account_id=ACCT,
        intent="OPEN",
        round=0,
        attempt=1,
        status="accepted",
        order_id=None,
        order_no="A1",
        qty=1,
        filled_qty=0,
        price=Decimal("350.5"),
        order_type="LIMIT",
        sent_at=None,
        accepted_at=None,
        created_at="t",
    )
    base.update(over)
    return FoLegRow(**base)


def orderable(ord_qty=10, cash_=Decimal("50000000"), tot=None, lqd=0) -> FoOrderable:
    return FoOrderable(
        tot_qty=ord_qty if tot is None else tot,
        lqd_qty=lqd,
        ord_qty=ord_qty,
        cash=cash_,
        bass_idx=None,
    )


def portfolio(**over) -> FoPortfolio:
    base = dict(
        account_id=ACCT,
        nav=NAV,
        cash=cash(),
        positions=[],
        legs=[],
        blocking=None,
        halted=False,
        verified={"FUTURES": True, "OPTION_BUY": True, "OPTION_SELL": True},
        realized_today=Decimal(0),
    )
    base.update(over)
    return FoPortfolio(**base)


def gate(d: FoDecision | None = None, **over) -> GateInput:
    d = d or decision()
    base = dict(
        decision=d,
        env=FoDecisionEnvelope(decision=d, request_id="r1"),
        port=portfolio(),
        cfg=cfg(),
        quote=quote(),
        underlying=Decimal("350"),
        index_asof=NOW,
        regime="neutral",
        view_state="NONE",
        now=NOW,
        today=TODAY,
        orderable=orderable(),
        contract=contract(),
        days_left=16,
    )
    base.update(over)
    return GateInput(**base)


__all__ = ["replace"]
