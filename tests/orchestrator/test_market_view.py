"""시장 세션 묶음(orchestrator/market_view): 입력 조립·세션 1회·오늘 제안 읽기."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from helpers_sources import T0

from trader.agents.registry import MARKET_AGENTS
from trader.broker.models import Candle
from trader.config import AccountConfig, HedgeCfg, Settings
from trader.control import control
from trader.orchestrator import market_view, session
from trader.orchestrator.run_store import market_dir, read_checkpoint

D = T0.date()


@pytest.fixture
def accounts():
    return [
        AccountConfig(id="paper-main", env="paper", cano="1", markets=["KR", "US"], mode="auto")
    ]


class _Market:
    def get_daily_kr(self, ticker, days=260):
        from datetime import timedelta

        return [
            Candle(
                date=D - timedelta(days=i),
                open=Decimal(100),
                high=Decimal(101),
                low=Decimal(99),
                close=Decimal(100 - i),
                volume=1,
            )
            for i in range(5, -1, -1)
        ]


def test_build_inputs_prefers_news_store_over_google(project, conn, accounts, respx_mock):
    """저장소에 최근 뉴스가 있으면 구글 질의를 부르지 않고 태그 종목까지 싣는다 (Phase 19)."""
    from datetime import timedelta

    from trader.sources import news_store

    route = respx_mock.get(url__startswith="https://news.google.com").mock(
        return_value=__import__("httpx").Response(500)
    )
    news_store.insert_items(
        conn,
        [
            {
                "title": "코스피 급락",
                "url": "https://a/1",
                "summary": "삼성전자 약세",
                "published_at": (T0 - timedelta(hours=3)).isoformat(),
                "received_at": (T0 - timedelta(hours=3)).isoformat(),
            }
        ],
        market="KR",
        source="google_rss",
        query="증시",
        lang="ko",
        names={"005930": "삼성전자"},
    )
    inp = market_view.build_inputs(conn, "KR", D, settings=Settings(), asof=T0, now=lambda: T0)
    assert [n["title"] for n in inp["macro_news"]] == ["코스피 급락"]
    assert inp["macro_news"][0]["tickers"] == ["005930"]
    assert route.call_count == 0 and "macro_news" not in inp["errors"]


def test_build_inputs_collects_regime_index_and_notes(project, conn, accounts):
    control.set_flag(
        conn,
        "regime:KR",
        {"state": "bear", "count": 2, "signals": {"ma_below": True, "close": 95.0}},
    )
    inp = market_view.build_inputs(
        conn,
        "KR",
        D,
        settings=Settings(),
        asof=T0,
        market_client=_Market(),
        accounts=accounts,
        now=lambda: T0,
    )
    assert inp["regime"]["state"] == "bear" and inp["regime"]["values"]["close"] == 95.0
    assert inp["index"]["symbol"] == "069500" and len(inp["index"]["closes"]) == 6
    assert inp["proxy"] == "KOSPI200" and inp["hedge"]["max_weight_pct"] == 0.2
    assert inp["market_notes"] == [] and isinstance(inp["errors"], dict)
    p = market_view.save_inputs(inp, market="KR", today=D, state=None)
    assert p.name == "_market_inputs.json"
    assert read_checkpoint(market_dir(D, "KR"), "_market_inputs")["data"]["market"] == "KR"


def test_run_market_view_fake_bear_gives_risk_off(project, conn, accounts, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    control.set_flag(conn, "regime:KR", {"state": "bear", "count": 3, "signals": {}})
    inp = market_view.build_inputs(conn, "KR", D, settings=Settings(), asof=T0, now=lambda: T0)
    out = market_view.run_market_view(
        Settings(), conn, accounts, market="KR", date=D, inputs=inp, cap_hit=False, now=lambda: T0
    )
    assert out["view"]["stance"] == "RISK_OFF" and out["view"]["hedge_weight"] == 0.1
    assert out["session"]["ok"] and out["session"]["fake"]
    run = conn.execute("SELECT kind, status FROM runs").fetchone()
    assert (run["kind"], run["status"]) == ("market", "ok")
    assert market_view.load_view(conn, "KR", D)["stance"] == "RISK_OFF"
    assert market_view.load_view(conn, "KR", D.replace(day=D.day + 1)) is None


def test_run_market_view_skips_on_cap_or_disabled(project, conn, accounts, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    inp = market_view.build_inputs(conn, "KR", D, settings=Settings(), asof=T0, now=lambda: T0)
    out = market_view.run_market_view(
        Settings(), conn, accounts, market="KR", date=D, inputs=inp, cap_hit=True, now=lambda: T0
    )
    assert out["skipped"] == "세션 상한" and out["view"] is None
    s = Settings(hedge=HedgeCfg(market_session=False))
    out2 = market_view.run_market_view(
        s, conn, accounts, market="KR", date=D, inputs=inp, cap_hit=False, now=lambda: T0
    )
    assert "market_session" in out2["skipped"]
    assert conn.execute("SELECT COUNT(*) FROM agent_calls").fetchone()[0] == 0


def test_market_session_uses_market_agents(project, conn, accounts, monkeypatch):
    monkeypatch.delenv("FAKE_CLAUDE", raising=False)
    inp = market_view.build_inputs(conn, "KR", D, settings=Settings(), asof=T0, now=lambda: T0)
    market_view.save_inputs(inp, market="KR", today=D, state=None)
    seen = {}

    def runner(cmd, **kw):
        seen["agents"] = json.loads((Path(kw["cwd"]) / "agents.json").read_text(encoding="utf-8"))
        seen["stdin"] = json.loads(kw["input"])
        from subprocess import CompletedProcess

        return CompletedProcess(
            [],
            0,
            stdout=json.dumps(
                {"type": "result", "is_error": False, "result": "{}", "total_cost_usd": 0.1}
            ),
            stderr="",
        )

    repo_agents = Path(__file__).resolve().parents[2] / ".claude" / "agents"
    res = session.run_session(
        session.SessionSpec(market="KR", date=D, tickers=[], dry_run=True, asof=T0, kind="market"),
        Settings(),
        conn,
        accounts,
        runner=runner,
        claude_bin=Path("/x/claude"),
        agents_dir=repo_agents,
        now=lambda: T0,
    )
    assert res.ok
    assert set(seen["agents"]) == set(MARKET_AGENTS)
    assert seen["stdin"]["kind"] == "market" and seen["stdin"]["market"] == "KR"
    assert conn.execute("SELECT kind FROM runs").fetchone()["kind"] == "market"


def test_clamp_weight():
    cfg = HedgeCfg(min_weight_pct=0.05, max_weight_pct=0.2)
    assert market_view.clamp_weight(None, cfg) == 0.2
    assert market_view.clamp_weight({"hedge_weight": 0.12}, cfg) == 0.12
    assert market_view.clamp_weight({"hedge_weight": 0.01}, cfg) == 0.05
    assert market_view.clamp_weight({"hedge_weight": 0.25}, cfg) == 0.2
    with pytest.raises(ValueError):
        HedgeCfg(min_weight_pct=0.3, max_weight_pct=0.2)


def test_build_inputs_without_regime_or_client_is_empty_but_ok(project, conn):
    inp = market_view.build_inputs(
        conn, "US", D, settings=Settings(), asof=datetime(2026, 10, 24, 12, tzinfo=UTC)
    )
    assert inp["regime"]["state"] is None and inp["index"]["closes"] == []
    assert inp["proxy"] == "SPX"


# --- 보완: 제안 적중률 복기·시장 세션 상한 ------------------------------------------


def _record(conn, d, stance, horizon=3, market="KR"):
    market_view.record_view(
        conn,
        today=d,
        market=market,
        view={"stance": stance, "hedge_weight": 0.1, "horizon_days": horizon, "confidence": 0.6},
    )


def test_record_and_mature_views_scores_direction(conn):
    from datetime import timedelta

    d0 = D
    _record(conn, d0, "RISK_OFF")
    _record(conn, d0 + timedelta(days=1), "RISK_ON")
    _record(conn, d0 + timedelta(days=2), "NEUTRAL")
    _record(conn, d0 + timedelta(days=3), "RISK_OFF")  # 만기 전
    _record(conn, d0, "RISK_OFF")  # 같은 날 재제출 → 덮어쓰기(행 1개)
    assert conn.execute("SELECT COUNT(*) FROM market_views").fetchone()[0] == 4
    # 종가: 100, 99, 98, 97, 96.5, 96.3 (하락 지속)
    closes = [100, 99, 98, 97, 96.5, 96.3]
    candles = [(d0 + timedelta(days=i), Decimal(str(c))) for i, c in enumerate(closes)]
    n = market_view.mature_views(conn, "KR", today=d0 + timedelta(days=5), candles=candles)
    assert n == 3  # 마지막 제안은 만기 봉(d0+6)이 없어 미채점
    rows = {
        r["date"]: dict(r)
        for r in conn.execute("SELECT date, hit, index_return FROM market_views ORDER BY date")
    }
    assert rows[d0.isoformat()]["hit"] == 1  # RISK_OFF ∧ 하락
    assert rows[(d0 + timedelta(days=1)).isoformat()]["hit"] == 0  # RISK_ON ∧ 하락
    assert rows[(d0 + timedelta(days=2)).isoformat()]["hit"] == 1  # NEUTRAL ∧ |−1.7%| ≤ 2%
    assert rows[(d0 + timedelta(days=3)).isoformat()]["hit"] is None
    sc = market_view.scorecard(conn, "KR")
    assert sc["n"] == 4 and sc["matured"] == 3 and sc["hits"] == 2
    assert abs(sc["hit_rate"] - 2 / 3) < 1e-9
    assert sc["by_stance"]["RISK_OFF"]["matured"] == 1 and sc["by_stance"]["RISK_OFF"]["hits"] == 1
    assert sc["by_stance"]["RISK_ON"]["avg_return"] < 0
    # 다시 돌려도 채점된 행은 건드리지 않는다
    assert market_view.mature_views(conn, "KR", today=d0 + timedelta(days=5), candles=candles) == 0
    assert market_view.scorecard(conn, "US")["n"] == 0


def test_mature_views_does_not_look_into_future(conn):
    from datetime import timedelta

    _record(conn, D, "RISK_OFF", horizon=2)
    candles = [(D + timedelta(days=i), 100 - i) for i in range(5)]
    # 만기 봉(D+2)이 today(D+1) 뒤 → 미채점, today 가 지나면 채점
    assert market_view.mature_views(conn, "KR", today=D + timedelta(days=1), candles=candles) == 0
    assert market_view.mature_views(conn, "KR", today=D + timedelta(days=2), candles=candles) == 1


def test_submit_market_view_records_ledger_row(project, conn, accounts, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    control.set_flag(conn, "regime:KR", {"state": "bear", "count": 2, "signals": {}})
    inp = market_view.build_inputs(conn, "KR", D, settings=Settings(), asof=T0, now=lambda: T0)
    market_view.run_market_view(
        Settings(), conn, accounts, market="KR", date=D, inputs=inp, cap_hit=False, now=lambda: T0
    )
    row = conn.execute("SELECT stance, hedge_weight, date FROM market_views").fetchone()
    assert (row["stance"], row["hedge_weight"], row["date"]) == ("RISK_OFF", 0.1, D.isoformat())
    assert market_view.market_sessions_today(conn, T0) == 1
    assert market_view.market_sessions_today(None, T0) == 0


def test_regime_summary_and_status_include_scorecard(project, conn):
    from datetime import timedelta

    from trader.risk import regime

    assert regime.summary(conn, ["KR"])[0]["scorecard"] is None
    _record(conn, D, "RISK_OFF", horizon=1)
    market_view.mature_views(
        conn, "KR", today=D + timedelta(days=2), candles=[(D, 100), (D + timedelta(days=1), 95)]
    )
    sc = regime.summary(conn, ["KR"])[0]["scorecard"]
    assert sc["hits"] == 1 and sc["matured"] == 1
