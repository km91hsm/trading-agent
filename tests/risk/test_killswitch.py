def test_auto_trip_scopes_to_account_manual_to_all(conn):
    """자동 발동은 발동한 계좌만 막고(모의 손실이 실전을 막지 않게), 수동 /kill 은 전 계좌."""
    from datetime import date
    from decimal import Decimal

    from trader.risk import killswitch

    kw = {"market": "KR", "today": date(2026, 10, 27), "limit_pct": 0.15}
    killswitch.observe(conn, account_id="paper-main", nav=Decimal("100"), **kw)
    assert killswitch.observe(conn, account_id="paper-main", nav=Decimal("80"), **kw)
    assert killswitch.reason(conn, "paper-main") and killswitch.reason(conn, "real-main") is None
    assert killswitch.reason(conn)  # 표시용은 켜져 있음
    killswitch.engage(conn, reason="/kill", source="telegram")
    assert killswitch.reason(conn, "real-main")


def test_auto_engage_does_not_shrink_manual_kill(conn):
    from trader.risk import killswitch

    killswitch.engage(conn, reason="/kill", source="telegram")
    killswitch.engage(conn, reason="자동", source="auto", account_id="paper-main")
    assert killswitch.state(conn)["accounts"] is None  # 전 계좌 그대로
    assert not conn.in_transaction
