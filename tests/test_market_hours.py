from datetime import UTC, datetime

from trader.market_hours import is_open


def test_kr_regular_session():
    assert is_open("KR", datetime(2026, 10, 9, 6, 0, tzinfo=UTC))  # 금 15:00 KST
    assert not is_open("KR", datetime(2026, 10, 9, 7, 0, tzinfo=UTC))  # 16:00 KST
    assert not is_open("KR", datetime(2026, 10, 10, 2, 0, tzinfo=UTC))  # 토요일


def test_us_regular_session_and_holiday():
    assert is_open("US", datetime(2026, 10, 9, 14, 0, tzinfo=UTC))  # 금 10:00 ET
    assert not is_open("US", datetime(2026, 10, 9, 21, 0, tzinfo=UTC))  # 17:00 ET
    assert not is_open("US", datetime(2026, 12, 25, 15, 0, tzinfo=UTC))  # 성탄절


def test_us_early_close_ends_session_at_13_et():
    from datetime import UTC, datetime

    from trader import market_hours

    # 2026-11-27 (추수감사절 다음 날) 13:30 ET = 18:30 UTC
    t = datetime(2026, 11, 27, 18, 30, tzinfo=UTC)
    assert market_hours.close_hm("US", t.date()) == "13:00"
    assert not market_hours.is_open("US", t) and market_hours.session_over("US", t)
    normal = datetime(2026, 11, 25, 18, 30, tzinfo=UTC)
    assert market_hours.is_open("US", normal) and not market_hours.session_over("US", normal)


def test_completed_candles_drops_today_bar_until_session_over():
    """전체 검토 2차 2-P4(L7): 장중의 오늘 봉은 거래량이 있어도 미완성이라 버린다(마감 뒤 포함),
    미래 날짜·거래량 0 꼬리도 버린다."""
    from datetime import date
    from decimal import Decimal

    from trader.broker.models import Candle
    from trader.market_hours import completed_candles

    def c(d, vol=10):
        one = Decimal(1)
        return Candle(date=d, open=one, high=one, low=one, close=one, volume=vol)

    cs = [c(date(2026, 10, 8)), c(date(2026, 10, 9))]
    assert completed_candles("KR", cs, datetime(2026, 10, 9, 1, 0, tzinfo=UTC)) == cs[:1]  # 10:00
    assert completed_candles("KR", cs, datetime(2026, 10, 9, 7, 0, tzinfo=UTC)) == cs  # 16:00
    assert completed_candles("US", cs, datetime(2026, 10, 9, 14, 0, tzinfo=UTC)) == cs[:1]
    assert completed_candles("US", cs, datetime(2026, 10, 9, 21, 0, tzinfo=UTC)) == cs
    future = cs + [c(date(2026, 10, 12))]
    assert completed_candles("KR", future, datetime(2026, 10, 9, 7, 0, tzinfo=UTC)) == cs
    stub = cs + [c(date(2026, 10, 12), vol=0)]
    assert completed_candles("KR", stub, datetime(2026, 10, 12, 7, 0, tzinfo=UTC)) == cs
