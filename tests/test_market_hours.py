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
