"""Phase 26 단계 1: 파생 TR 표·도메인 모델."""

from datetime import UTC, datetime
from decimal import Decimal

import pydantic
import pytest

from trader.broker import tr_ids
from trader.broker.errors import KisApiError
from trader.broker.models import FoCash, FoFill, FoOrderState, FoPosition, FoQuote


def test_fo_tr_ids_paper():
    assert tr_ids.tr_id("FO_ORDER", "paper") == "VTTO1101U"
    assert tr_ids.tr_id("FO_ORDER", "real") == "TTTO1101U"
    assert tr_ids.tr_id("FO_ORDER_CANCEL", "paper") == "VTTO1103U"
    assert tr_ids.tr_id("FO_CCLD", "paper") == "VTTO5201R"
    assert tr_ids.tr_id("FO_PSBL_ORDER", "paper") == "VTTO5105R"
    assert tr_ids.tr_id("FO_BALANCE", "paper") == "VTFO6118R"
    assert tr_ids.tr_id("FO_BALANCE", "real") == "CTFO6118R"
    assert tr_ids.tr_id("FO_PRICE", "paper") == "FHMIF10000000"
    assert tr_ids.tr_id("FO_ORDERBOOK", "real") == "FHMIF10010000"
    assert tr_ids.tr_id("FO_DAILY", "real") == "FHKIF03020100"
    assert tr_ids.tr_id("FO_MINUTE", "real") == "FHKIF03020200"
    assert tr_ids.path("FO_ORDER") == "/uapi/domestic-futureoption/v1/trading/order"
    assert tr_ids.path("FO_BALANCE") == "/uapi/domestic-futureoption/v1/trading/inquire-balance"


def test_fo_night_unsupported_on_paper():
    for name in ("FO_ORDER_NIGHT", "FO_ORDER_CANCEL_NIGHT", "FO_CCLD_NIGHT", "FO_BALANCE_NIGHT"):
        with pytest.raises(KisApiError) as e:
            tr_ids.tr_id(name, "paper")
        assert e.value.msg_cd == "UNSUPPORTED"
    assert tr_ids.tr_id("FO_ORDER_NIGHT", "real") == "STTN1101U"


def test_fo_position_requires_side():
    base = dict(
        code="105W09", name="미니", kind="FUTURES", qty=1, avg_price=Decimal("350"),
        current_price=Decimal("351"), pnl=Decimal("50000"), sellable_qty=1,
    )
    with pytest.raises(pydantic.ValidationError, match="contract_side"):
        FoPosition(**base)
    with pytest.raises(pydantic.ValidationError):
        FoPosition(**{**base, "qty": 0}, contract_side="LONG")
    p = FoPosition(**base, contract_side="SHORT")
    assert p.strike is None and p.expiry is None
    with pytest.raises(pydantic.ValidationError):
        FoPosition(**base, contract_side="SHORT", foo=1)


def test_fo_cash_nav_optional_and_times():
    t = datetime(2026, 10, 27, 0, 0, tzinfo=UTC)
    c = FoCash(
        deposit=Decimal("1"), orderable_cash=Decimal("1"), orderable_total=Decimal("1"),
        margin_total=Decimal("0"), add_margin=Decimal("0"), nav=None,
        realized_today=Decimal("0"), unrealized=Decimal("0"), requested_at=t, received_at=t,
    )
    assert c.nav is None


def test_fo_fill_and_order_state_shapes():
    f = FoFill(
        order_no="1", orgn_order_no=None, code="105W09", side="BUY", remaining_qty=0,
        filled_qty=2, rejected_qty=0, cancelled_qty=0, filled_amount=Decimal("35000000"),
        avg_price=Decimal("350"), filled_at=datetime(2026, 10, 27, 0, 0, tzinfo=UTC),
    )
    assert f.filled_qty == 2
    s = FoOrderState(
        order_no="1", order_qty=2, filled_qty=2, remaining_qty=0, rejected_qty=0,
        cancelled_qty=0, terminal=True, state="filled",
    )
    assert s.terminal


def test_fo_quote_asof_distinct_from_received():
    t = datetime(2026, 10, 27, 0, 0, tzinfo=UTC)
    q = FoQuote(
        code="105W09", kind="FUTURES", price=Decimal("350"), bid=None, ask=None,
        change_pct=0.1, volume=10, open_interest=None, basis=None, upper=None, lower=None,
        last_trade_date=None, days_left=None, hist_vol=None, iv=None, delta=None, gamma=None,
        theta=None, vega=None, strike=None, underlying=None, underlying_asof=None,
        quote_asof=None, received_at=t,
    )
    assert q.quote_asof is None and q.received_at == t
