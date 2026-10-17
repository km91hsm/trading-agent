from datetime import date
from decimal import Decimal

import pytest
from conftest import make_account

from trader.broker.errors import KisApiError
from trader.broker.fake import FakeBroker, FakeMarket
from trader.broker.models import Candle, Cash, Position


def _candles(n):
    return [
        Candle(date=date(2026, 1, 1 + i), open=1, high=2, low=1, close=1 + i, volume=10)
        for i in range(n)
    ]


def test_fake_market_same_interface():
    m = FakeMarket(
        prices={"005930": Decimal("71500"), "AAPL": Decimal("227.55")},
        candles={"005930": _candles(3)},
    )
    assert m.get_price_kr("005930").price == Decimal("71500")
    assert m.get_price_us("AAPL", "NAS").market == "US"
    assert [c.close for c in m.get_daily_kr("005930", days=2)] == [Decimal(2), Decimal(3)]
    assert m.get_daily_us("AAPL", "NAS") == []
    assert m.is_holiday(date(2026, 10, 10)) is False
    assert m.calls == ["get_price_kr", "get_price_us", "get_daily_kr", "get_daily_us", "is_holiday"]


def test_fake_fail_injection():
    m = FakeMarket(prices={}, fail={"get_price_kr"})
    with pytest.raises(KisApiError):
        m.get_price_kr("005930")


def test_fake_broker():
    acc = make_account("paper-main", "paper", "auto", ["KR", "US"])
    pos = [
        Position(
            ticker="005930",
            market="KR",
            qty=1,
            avg_price=1,
            current_price=1,
            eval_amount=1,
            pnl_pct=0.0,
        )
    ]
    b = FakeBroker(acc, positions=pos, cash={"KR": Cash(currency="KRW", available=1, total=1)})
    assert b.get_positions("KR") == pos and b.get_positions("US") == []
    assert b.get_cash("KR").currency == "KRW"
    with pytest.raises(KisApiError):
        b.get_cash("US")  # 설정 안 된 시장은 실패
