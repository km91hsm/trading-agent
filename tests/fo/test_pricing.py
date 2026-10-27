from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trader.broker.models import FoQuote
from trader.fo import pricing


def test_black76_put_call_parity():
    F, K = Decimal("350"), Decimal("345")
    c = pricing.price("CALL", F, K, 0.2, 20)
    p = pricing.price("PUT", F, K, 0.2, 20)
    assert abs((c - p) - (F - K)) < Decimal("0.001")
    assert c > 5 and p > 0


def test_black76_expiry_intrinsic():
    assert pricing.price("CALL", Decimal("350"), Decimal("345"), 0.2, 0) == Decimal("5")
    assert pricing.price("PUT", Decimal("350"), Decimal("345"), 0.2, 0) == Decimal("0")
    assert pricing.price("FUTURES", Decimal("350"), Decimal("0"), 0.2, 5) == Decimal("350")


def test_black76_iv_monotone():
    a = pricing.price("CALL", Decimal("350"), Decimal("350"), 0.15, 10)
    b = pricing.price("CALL", Decimal("350"), Decimal("350"), 0.30, 10)
    assert b > a


def _q(kind, **kw):
    base = dict(
        code="x",
        kind=kind,
        price=Decimal("5"),
        bid=None,
        ask=None,
        change_pct=0.0,
        volume=0,
        open_interest=None,
        basis=None,
        upper=None,
        lower=None,
        last_trade_date=None,
        days_left=10,
        hist_vol=None,
        iv=20.0,
        delta=None,
        gamma=None,
        theta=None,
        vega=None,
        strike=Decimal("350"),
        underlying=Decimal("350"),
        underlying_asof=None,
        quote_asof=None,
        received_at=datetime(2026, 10, 27, tzinfo=UTC),
    )
    base.update(kw)
    return FoQuote(**base)


def test_reprice_futures_linear_and_option_nonlinear():
    f = _q("FUTURES", price=Decimal("350"), strike=None, iv=None)
    assert pricing.reprice(f, index_shift_pct=-0.03, iv_rel=0.5, days_left=10) == Decimal("339.5")
    c = _q("CALL")
    base = pricing.price("CALL", Decimal("350"), Decimal("350"), 0.2, 10)
    up = pricing.reprice(c, index_shift_pct=0.03, iv_rel=0.0, days_left=10)
    assert up > base
    with pytest.raises(ValueError):
        pricing.reprice(_q("CALL", iv=None), index_shift_pct=0.0, iv_rel=0.0, days_left=10)
