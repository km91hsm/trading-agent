from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trader.broker.models import Candle, OrderRequest, Position, Quote


def test_quote_decimal_roundtrip():
    q = Quote(
        ticker="005930",
        market="KR",
        price="71500",
        change_pct=0.5,
        volume=1,
        asof=datetime(2026, 10, 9, tzinfo=UTC),
    )
    assert q.price == Decimal("71500")
    assert Quote.model_validate(q.model_dump(mode="json")).price == Decimal("71500")


def test_candle_extra_forbidden():
    with pytest.raises(ValidationError):
        Candle(date=date(2026, 10, 9), open=1, high=1, low=1, close=1, volume=1, foo=1)


def test_position_pnl_pct_float():
    p = Position(
        ticker="AAPL",
        market="US",
        qty=3,
        avg_price="100",
        current_price="110",
        eval_amount="330",
        pnl_pct=0.1,
    )
    assert p.eval_amount == Decimal("330")


def test_order_request_requires_explicit_exchange():
    with pytest.raises(ValidationError):
        OrderRequest(ticker="AAPL", market="US", side="BUY", qty=1, order_type="LIMIT", price="1")
    o = OrderRequest(
        ticker="005930",
        market="KR",
        side="BUY",
        qty=1,
        order_type="LIMIT",
        price="70000",
        exchange=None,
    )
    assert o.exchange is None
