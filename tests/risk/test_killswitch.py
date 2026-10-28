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


def test_unexplained_nav_jump_flags_flow_suspicion_without_touching_base(conn):
    """2-O16: 보유 종목 가격 변화로 설명되지 않는 NAV 변화(입출금 의심)는 기록만 하고 기준·판정은
    그대로 둔다. 가격 변화로 설명되는 변화·작은 잔차는 의심이 아니다."""
    from datetime import date
    from decimal import Decimal as D

    from trader.risk import killswitch

    kw = {"market": "KR", "today": date(2026, 10, 27), "limit_pct": 0.15}
    pos = [("005930", 10, D("100"))]  # 평가 1,000 + 현금 9,000
    killswitch.observe(conn, account_id="paper-main", nav=D("10000"), positions=pos, **kw)
    assert killswitch.flow_suspicion(conn, "paper-main", "KR") is None
    # 가격이 100 → 150: NAV +500 은 설명됨
    pos2 = [("005930", 10, D("150"))]
    killswitch.observe(conn, account_id="paper-main", nav=D("10500"), positions=pos2, **kw)
    assert killswitch.flow_suspicion(conn, "paper-main", "KR") is None
    # 가격 그대로인데 NAV 가 2,500 줄었다 — 출금 의심(잔차 −2,500 ≥ 5%)
    killswitch.observe(conn, account_id="paper-main", nav=D("8000"), positions=pos2, **kw)
    got = killswitch.flow_suspicion(conn, "paper-main", "KR")
    assert got is not None and D(got["residual"]) == D("-2500") and got["date"] == "2026-10-27"
    assert killswitch.reason(conn, "paper-main")  # 판정은 기준 그대로(−20%) — 그래서 특보로 알린다
    assert D(killswitch.control.get_flag(conn, "killswitch_base:paper-main:KR")["nav"]) == D(
        "10000"
    )
    # 사람이 기준을 다시 잡으면 의심 기록도 지운다
    killswitch.release(conn, note="출금 2,500 확인 — 기준 재설정")
    assert killswitch.flow_suspicion(conn, "paper-main", "KR") is None
