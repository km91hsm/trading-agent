from datetime import datetime

from trader import market_hours
from trader.config import KST


def _at(h, m, day=27):  # 2026-10-27 화요일
    return datetime(2026, 10, day, h, m, tzinfo=KST)


def test_fo_session_0845_open_1545_close():
    assert market_hours.FO_SESSION == ("08:45", "15:45")
    assert not market_hours.is_fo_open(_at(8, 44))
    assert market_hours.is_fo_open(_at(8, 45))
    assert market_hours.is_fo_open(_at(15, 45))
    assert not market_hours.is_fo_open(_at(15, 46))
    assert not market_hours.is_fo_open(_at(10, 0, day=31))  # 토요일


def test_last_trade_day_closes_1520():
    assert market_hours.fo_close_hm(False) == "15:45"
    assert market_hours.fo_close_hm(True) == "15:20"
    assert market_hours.is_fo_open(_at(15, 20), last_trade_day=True)
    assert not market_hours.is_fo_open(_at(15, 21), last_trade_day=True)


def test_index_live_window():
    assert not market_hours.index_live(_at(8, 59))
    assert market_hours.index_live(_at(9, 0))
    assert market_hours.index_live(_at(15, 30))
    assert not market_hours.index_live(_at(15, 31))
    assert not market_hours.index_live(_at(10, 0, day=31))
