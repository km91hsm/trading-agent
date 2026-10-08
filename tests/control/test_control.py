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
