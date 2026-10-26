"""Stock 교훈 1차 — 게이트: 누적 손실 킬스위치(D-049), 실전 자본 상한(D-074), 매매 불가·경고
종목 차단(D-030)."""

from datetime import date
from decimal import Decimal

import pytest
from conftest import make_account
from helpers_agents import ok_decision
from test_rules import _eval, _portfolio, _pos

from trader.broker.fake import FakeBroker
from trader.broker.models import Cash
from trader.control import control
from trader.orchestrator import executor
from trader.risk import killswitch, tradability

D = date(2026, 10, 27)


# --- 킬스위치 ---------------------------------------------------------------


def _snap(conn, nav, d, acct="paper-main", market="KR"):
    conn.execute(
        "INSERT INTO positions_snapshot(account_id, market, date, ticker, qty, avg_price, "
        "current_price, eval_amount, nav, cash, created_at) VALUES (?,?,?,'',0,'0','0','0',?,?,"
        "'2026-10-01T00:00:00+00:00')",
        (acct, market, d, str(nav), str(nav)),
    )
    conn.commit()


def test_killswitch_engages_on_drawdown_from_start(conn):
    _snap(conn, 10_000_000, "2026-09-01")
    assert (
        killswitch.observe(
            conn,
            account_id="paper-main",
            market="KR",
            nav=Decimal(9_000_000),
            today=D,
            limit_pct=0.15,
        )
        is False
    )
    assert killswitch.reason(conn) is None
    assert (
        killswitch.observe(
            conn,
            account_id="paper-main",
            market="KR",
            nav=Decimal(8_400_000),
            today=D,
            limit_pct=0.15,
        )
        is True
    )
    r = killswitch.reason(conn)
    assert r and "paper-main KR" in r and "-16.0%" in r
    # 이미 켜져 있으면 다시 '새로 켜짐' 이 아니다
    assert (
        killswitch.observe(
            conn,
            account_id="paper-main",
            market="KR",
            nav=Decimal(8_000_000),
            today=D,
            limit_pct=0.15,
        )
        is False
    )


def test_killswitch_release_needs_note_and_rebases(conn):
    _snap(conn, 10_000_000, "2026-09-01")
    _snap(conn, 8_000_000, "2026-10-27")
    killswitch.observe(
        conn, account_id="paper-main", market="KR", nav=Decimal(8_000_000), today=D, limit_pct=0.15
    )
    with pytest.raises(ValueError):
        killswitch.release(conn, note="짧음")
    killswitch.release(conn, note="손실 원인 확인: 반도체 급락, 비중 축소 완료")
    assert killswitch.reason(conn) is None
    # 해제 시점 NAV 가 새 기준 — 같은 NAV 로 바로 다시 켜지지 않는다
    assert (
        killswitch.observe(
            conn,
            account_id="paper-main",
            market="KR",
            nav=Decimal(8_000_000),
            today=D,
            limit_pct=0.15,
        )
        is False
    )
    assert control.get_flag(conn, killswitch.FLAG)["released_note"].startswith("손실 원인")


def test_killswitch_manual_engage(conn):
    killswitch.engage(conn, reason="텔레그램 /kill", source="telegram")
    assert "텔레그램" in killswitch.reason(conn)


def test_rules_killswitch_blocks_new_buys_only():
    buy = ok_decision(action="BUY", target_weight=0.1)
    v = _eval(buy, killswitch="누적 손실 −16%")
    assert not v.approved and any("킬스위치" in r for r in v.reasons)
    sell = ok_decision(action="SELL", target_weight=0.0, ticker="000660")
    pf = _portfolio(positions={"000660": _pos()}, held_since={"000660": date(2026, 10, 1)})
    v2 = _eval(sell, pf, killswitch="누적 손실 −16%")
    assert not any("킬스위치" in r for r in v2.reasons)
    hedge = _eval(buy, is_hedge=True, killswitch="x")
    assert not any("킬스위치" in r for r in hedge.reasons)


# --- 매매 불가·경고 종목 --------------------------------------------------------


@pytest.mark.parametrize(
    ("quote", "expected"),
    [
        ({"status_code": "51"}, "관리종목"),
        ({"status_code": "58"}, "거래정지"),
        ({"warn_code": "02"}, "투자경고"),
        ({"warn_code": "03"}, "투자위험"),
        ({"halted": True}, "임시정지"),
        ({"status_code": "55", "warn_code": "00"}, None),  # 55 신용가능 — 정상
        ({}, None),
        (None, None),
    ],
)
def test_block_reason(quote, expected):
    got = tradability.block_reason(quote)
    assert (got is None) if expected is None else (expected in got)


def test_rules_trade_block_rejects_buy_not_sell():
    v = _eval(ok_decision(action="BUY", target_weight=0.1), trade_block="투자경고 종목")
    assert not v.approved and any("매매 제한" in r for r in v.reasons)
    pf = _portfolio(positions={"000660": _pos()}, held_since={"000660": date(2026, 10, 1)})
    v2 = _eval(
        ok_decision(action="SELL", target_weight=0.0, ticker="000660"),
        pf,
        trade_block="투자경고 종목",
    )
    assert not any("매매 제한" in r for r in v2.reasons)


# --- 실전 자본 상한 ------------------------------------------------------------


def test_capital_cap_limits_sizing_nav_and_cash(conn, project):
    acct = make_account("real-main", "real", "off").model_copy(
        update={"capital_cap": {"KR": Decimal("3000000")}}
    )
    broker = FakeBroker(
        acct,
        positions=[_pos(qty=10, price="100000")],  # 평가 100만
        cash={"KR": Cash(currency="KRW", available=Decimal(9_000_000), total=Decimal(9_000_000))},
    )
    pf = executor.build_portfolio(conn, broker, acct, "KR", D)
    assert pf.nav == Decimal("3000000")
    assert pf.cash_available == Decimal("2000000")  # 상한 − 이미 투자한 금액
    # 원장 스냅샷(성과·킬스위치)은 실제 NAV 그대로
    row = conn.execute("SELECT nav FROM positions_snapshot WHERE account_id='real-main'").fetchone()
    assert Decimal(row["nav"]) == Decimal("10000000")


def test_no_cap_keeps_real_nav(conn, project):
    acct = make_account("paper-main", "paper", "auto")
    broker = FakeBroker(
        acct,
        cash={"KR": Cash(currency="KRW", available=Decimal(5_000_000), total=Decimal(5_000_000))},
    )
    pf = executor.build_portfolio(conn, broker, acct, "KR", D)
    assert pf.nav == Decimal("5000000") and pf.cash_available == Decimal("5000000")


def test_tools_trade_block_reads_inputs_quote():
    from trader.orchestrator import tools

    inputs = {"technical": {"quote": {"data": {"price": "1000", "status_code": "53"}}}}
    assert tools._trade_block(inputs) == "투자경고"
    assert tools._trade_block({"technical": {}}) is None


def test_kis_quote_carries_status_fields():
    from trader.broker.models import Quote

    q = Quote(
        ticker="005930",
        market="KR",
        price=Decimal(1),
        change_pct=0.0,
        volume=1,
        asof="2026-10-27T00:00:00+00:00",
        status_code="51",
        warn_code="00",
        halted=False,
    )
    dumped = q.model_dump(mode="json")
    assert tradability.block_reason(dumped) == "관리종목"


def test_health_reports_killswitch_critical(conn, project):
    from trader.config import Settings
    from trader.ops import health

    killswitch.engage(conn, reason="paper-main KR 누적 손실 -16.0%", source="auto")
    r = health.check(conn, Settings(), [make_account("paper-main", "paper", "auto")])
    assert r["areas"]["loss"] == "critical"
    assert any(i["key"] == "loss.killswitch" for i in r["items"])


def test_cli_killswitch_off_requires_note(conn, project):
    from typer.testing import CliRunner

    from trader.cli import app

    killswitch.engage(conn, reason="x", source="auto")
    r = CliRunner().invoke(app, ["control", "killswitch", "off"])
    assert r.exit_code == 1 and killswitch.reason(conn)
    r = CliRunner().invoke(
        app, ["control", "killswitch", "off", "--note", "원인 확인 후 해제합니다 (테스트)"]
    )
    assert r.exit_code == 0 and "꺼짐" in r.output and killswitch.reason(conn) is None
