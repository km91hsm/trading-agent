"""데몬 ↔ 테마 열기(Phase 25): 잡 등록·기록·상위 테마가 바뀔 때만 알림·API·CLI."""

import json
from datetime import UTC, date, datetime

from helpers_web import Sent
from test_daemon import SCHED, _deps
from typer.testing import CliRunner

from trader import daemon
from trader.broker.fake import FakeMarket
from trader.broker.models import RankRow
from trader.cli import app
from trader.config import Settings, ThemeCfg
from trader.control import control
from trader.scan import theme

NOW = datetime(2026, 10, 26, 2, 0, tzinfo=UTC)  # 월 11:00 KST


def _sectors(project):
    (project / "config" / "sectors.yaml").write_text(
        "sectors: [반도체, 2차전지, 기타]\nkr: {'111111': 반도체, '333333': 2차전지}\nus: {}\n",
        encoding="utf-8",
    )


def test_build_jobs_registers_theme_when_enabled():
    jobs = {j.id: j for j in daemon.build_jobs(SCHED, Settings(), markets=["KR", "US"])}
    assert jobs["kr_theme"].kind == "theme" and jobs["kr_theme"].interval_s == 1800
    off = daemon.build_jobs(SCHED, Settings(theme=ThemeCfg(enabled=False)), markets=["KR"])
    assert not any(j.kind == "theme" for j in off)


def test_theme_job_alerts_only_on_top_change(conn, project):
    _sectors(project)
    mc = FakeMarket(ranks={"volume": [RankRow(ticker="111111", rank=1)]})
    deps = _deps(conn, mc, now=lambda: NOW)
    deps.notifier = Sent()
    out = daemon._theme_body(deps, "KR")
    assert out["themes"] == 1 and out["alerted"] is True
    assert deps.notifier.msgs[-1].startswith("[테마 열기] KR\n1. 반도체(섹터)")
    n = len(deps.notifier.msgs)
    assert daemon._theme_body(deps, "KR")["alerted"] is False  # 같은 상위 테마 → 알림 없음
    mc.ranks["volume"] = [RankRow(ticker="333333", rank=1)]
    assert daemon._theme_body(deps, "KR")["alerted"] is True
    assert len(deps.notifier.msgs) == n + 1
    rows = conn.execute("SELECT theme, rank FROM theme_heat ORDER BY theme").fetchall()
    assert [r["theme"] for r in rows] == ["섹터:2차전지", "섹터:반도체"]
    assert control.get_flag(conn, "theme_heat:KR")["date"] == "2026-10-26"


def test_api_theme(client, conn):
    control.set_flag(
        conn,
        "theme_heat:KR",
        {
            "date": "2026-10-26",
            "observed_at": "x",
            "themes": [
                {"theme": "kw:로봇", "rank": 1, "score": 0.9, "tickers": [], "catalyst_ok": True}
            ],
        },
    )
    r = client.get("/api/system/theme", params={"market": "KR"}).json()
    assert r["enabled"] is True and r["themes"][0]["theme"] == "kw:로봇"
    assert client.get("/api/system/theme", params={"market": "XX"}).status_code == 400


def test_theme_show_cli_json(project, conn):
    control.set_flag(conn, "theme_heat:KR", {"market": "KR", "date": "2026-10-26", "themes": []})
    res = CliRunner().invoke(app, ["theme", "show", "--market", "KR"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["date"] == "2026-10-26"
    res = CliRunner().invoke(app, ["theme", "show", "--market", "US"])
    assert json.loads(res.output)["themes"] == []


def test_theme_map_uses_recorded_members(conn, project):
    _sectors(project)
    deps = _deps(
        conn, FakeMarket(ranks={"volume": [RankRow(ticker="111111", rank=1)]}), now=lambda: NOW
    )
    daemon._theme_body(deps, "KR")
    assert theme.theme_of_map(conn, "KR", date(2026, 10, 26), top_n=3) == {
        "111111": ["섹터:반도체"]
    }
