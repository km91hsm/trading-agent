"""아침 브리핑(Stock docs/06): 입력 묶음·FAKE 응답 전송·감속·끔."""

from datetime import UTC, datetime

from conftest import make_account
from helpers_web import Sent

from trader.config import Settings
from trader.orchestrator import briefing

NOW = datetime(2026, 10, 26, 22, 20, tzinfo=UTC)  # 화 07:20 KST
ACCTS = [make_account("paper-main", "paper", "auto", ["KR"])]


def test_inputs_shape(conn):
    conn.execute(
        "INSERT INTO positions_snapshot(account_id, market, date, ticker, qty, avg_price, "
        "current_price, eval_amount, nav, cash, created_at) VALUES ('paper-main','KR',"
        "'2026-10-26','005930',1,'0','0','300','1000','700','2026-10-26T07:00:00+00:00')"
    )
    conn.commit()
    p = briefing.build_inputs(conn, Settings(), ACCTS, now=NOW)
    assert set(p) == {
        "date",
        "us_overnight",
        "regime",
        "market_view",
        "news",
        "calendar",
        "holdings",
    }
    assert p["us_overnight"] is None  # 시세 클라이언트 없음
    assert p["holdings"] == [{"market": "KR", "ticker": "005930", "name": None, "weight": 0.3}]


def test_run_fake_sends_and_respects_switches(conn, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    sent = Sent()
    r = briefing.run(conn, Settings(), ACCTS, now=NOW, notifier=sent)
    assert r == {"lines": 1} and sent.msgs[-1].startswith("[아침 브리핑]")
    off = Settings(briefing={"enabled": False})
    assert briefing.run(conn, off, ACCTS, now=NOW, notifier=sent) == {"skipped": "꺼짐"}
    monkeypatch.setattr(
        "trader.usage.session_allowance", lambda *a, **k: (False, "구독 5시간 사용률 85%")
    )
    assert briefing.run(conn, Settings(), ACCTS, now=NOW, notifier=sent)["skipped"].startswith(
        "감속"
    )


def test_briefing_uses_kst_date(conn):
    p = briefing.build_inputs(conn, Settings(), ACCTS, now=NOW)  # 22:20Z = 다음 날 07:20 KST
    assert p["date"] == "2026-10-27"
