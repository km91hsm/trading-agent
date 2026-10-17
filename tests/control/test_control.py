import pytest
from conftest import make_account

from trader.control import control


def test_flag_roundtrip(conn):
    assert control.get_flag(conn, "paused", False) is False
    control.set_flag(conn, "paused", True)
    assert control.is_paused(conn) is True
    control.set_flag(conn, "paused", False)
    assert control.is_paused(conn) is False


def test_mode_default_from_yaml_then_control(conn):
    acc = make_account("paper-main", "paper", "auto")
    assert control.get_account_mode(conn, acc) == "auto"
    control.set_account_mode(conn, acc, "off")
    assert control.get_account_mode(conn, acc) == "off"


def test_real_auto_requires_confirm(conn):
    acc = make_account("real-main", "real", "off")
    with pytest.raises(control.ConfirmationRequired):
        control.set_account_mode(conn, acc, "auto")
    control.set_account_mode(conn, acc, "auto", confirm="LIVE")
    assert control.get_account_mode(conn, acc) == "auto"


def test_unknown_mode_rejected(conn):
    with pytest.raises(ValueError):
        control.set_account_mode(conn, make_account("p", "paper", "auto"), "yolo")


def test_watchlist_market_filter(conn):
    control.set_watchlist(conn, ["005930", "aapl", "US:BRK.B", "kr:000660"])
    assert control.get_watchlist(conn, market="KR") == ["005930", "000660"]
    assert control.get_watchlist(conn, market="US") == ["AAPL", "BRK.B"]
    assert control.get_watchlist(conn) == ["005930", "AAPL", "US:BRK.B", "KR:000660"]
    with pytest.raises(ValueError):
        control.set_watchlist(conn, ["JP:7203"])


def test_watchlist_canonical_and_remove(conn):
    assert control.set_watchlist(conn, ["005930", "KR:005930", "aapl", "US:AAPL"]) == [
        "005930",
        "AAPL",
    ]
    assert control.remove_watch(conn, "005930") == ["AAPL"]
    control.set_watchlist(conn, ["KR:000660"])
    assert control.remove_watch(conn, "000660") == []  # 접두사 없는 요청으로 접두사 항목 제거
