"""데몬 ↔ 공시 중요도(Phase 25 단계 6): critical 즉시 재분석·알림 1회, 운영 점검."""

from datetime import UTC, datetime, timedelta

from conftest import make_account
from helpers_web import Sent
from test_daemon_news import NOW, _queued, _trigger_deps

from trader import daemon
from trader.config import Settings
from trader.control import control
from trader.ops import health
from trader.sources import inputs


class FakeDart:
    def __init__(self, rows):
        self.rows = rows

    def market_disclosures(self, day, pages=3):
        return self.rows


def _row(code, name, no):
    return {
        "stock_code": code,
        "corp_name": "가나",
        "report_nm": name,
        "rcept_no": no,
        "rcept_dt": "20261024",
    }


def _deps(conn, rows):
    deps = _trigger_deps(conn)
    deps.notifier = Sent()
    deps.input_deps = inputs.InputDeps(market=None, dart=FakeDart(rows), edgar=None)
    return deps


def test_critical_disclosure_bypasses_min_novel(conn, project):
    """보유가 아니어도 유니버스 종목의 critical 공시 1건이면 바로 analyze_ticker 요청."""
    deps = _deps(conn, [_row("005930", "주권매매거래정지", "1"), _row("000660", "기업설명회", "2")])
    out = daemon._disclosure_trigger(deps, conn, "KR", deps.universe)
    assert out["inserted"] == 2 and out["critical_new"] == 1
    assert [p["ticker"] for k, p, s in _queued(conn) if k == "analyze_ticker"] == ["005930"]
    assert deps.notifier.msgs[-1].startswith("[중요 공시] KR")
    assert "재분석 요청 #" in deps.notifier.msgs[-1]


def test_critical_disclosure_alerts_once(conn, project):
    deps = _deps(conn, [_row("005930", "감자결정", "1")])
    daemon._disclosure_trigger(deps, conn, "KR", deps.universe)
    n = len(deps.notifier.msgs)
    out = daemon._disclosure_trigger(deps, conn, "KR", deps.universe)
    assert len(deps.notifier.msgs) == n and "critical_new" not in out
    assert control.get_flag(conn, "disclosure_seen:KR")


def test_critical_disclosure_paused_alerts_only(conn, project):
    control.set_flag(conn, "paused", True)
    deps = _deps(conn, [_row("005930", "상장폐지", "1")])
    daemon._disclosure_trigger(deps, conn, "KR", deps.universe)
    assert _queued(conn) == [] and "알림만" in deps.notifier.msgs[-1]


def test_health_disclosure_area_critical_for_held(conn, project):
    acct = make_account("paper-main", "paper", "auto", ["KR"])
    conn.execute(
        "INSERT INTO positions_snapshot(account_id, market, date, ticker, qty, avg_price, "
        "current_price, eval_amount, nav, cash, created_at) VALUES "
        "('paper-main','KR','2026-10-24','005930',1,'1','1','1','10','9',?)",
        (NOW.isoformat(),),
    )
    conn.commit()
    deps = _deps(conn, [_row("005930", "감자결정", "1")])
    daemon._disclosure_trigger(deps, conn, "KR", deps.universe)
    r = health.check(conn, Settings(markets=["KR"]), [acct], now=NOW)
    assert r["areas"]["disclosure"] == "critical"
    assert any(i["key"] == "disclosure.KR" for i in r["items"])
    # received_at 은 실제 적재 시각이라 '며칠 뒤' 도 실제 시각 기준(고정 날짜면 그날이 오면 깨진다)
    later = datetime.now(UTC) + timedelta(days=health.DISCLOSURE_DAYS + 1)
    r2 = health.check(conn, Settings(markets=["KR"]), [acct], now=later)
    assert r2["areas"]["disclosure"] == "ok"
