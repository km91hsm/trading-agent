"""데몬 news_collect 잡(Phase 19): 등록·시장 단위 격리·보존·큐 kind."""

from datetime import UTC, datetime, timedelta

from test_daemon import SCHED, _deps

from trader import daemon
from trader import jobs as jobq
from trader.broker.fake import FakeMarket
from trader.config import Settings, SourcesConfig
from trader.sources import news_store

NOW = datetime(2026, 10, 24, 1, 0, tzinfo=UTC)


def _cfg():
    return SourcesConfig.model_validate({"edgar": {"user_agent": "t t@e.com"}})


def test_build_jobs_includes_news_collect_interval():
    jobs = {j.id: j for j in daemon.build_jobs(SCHED, Settings(), markets=["KR"])}
    assert jobs["news_collect"].kind == "news_collect"
    assert jobs["news_collect"].interval_s == SCHED.news_collect.interval_s == 1800
    assert not jobq.is_heavy("news_collect")


def test_news_body_collects_per_market_and_prunes(conn, monkeypatch):
    deps = _deps(conn, FakeMarket(), now=lambda: NOW)
    deps.sources_cfg = _cfg()
    deps.settings.discovery.intraday.enabled = False  # 장중 발굴은 아래 전용 테스트
    seen = []

    def fake_collect(conn_, cfg, secrets, *, market, names, now, http=None):
        seen.append(market)
        if market == "US":
            raise RuntimeError("rss down")
        news_store.insert_items(
            conn_,
            [
                {
                    "title": "옛 기사",
                    "url": "https://a/old",
                    "published_at": (NOW - timedelta(days=20)).isoformat(),
                    "received_at": (NOW - timedelta(days=20)).isoformat(),
                },
                {
                    "title": "새 기사",
                    "url": "https://a/new",
                    "published_at": NOW.isoformat(),
                    "received_at": NOW.isoformat(),
                },
            ],
            market=market,
            source="t",
            query="q",
            lang="ko",
            names={},
        )
        return {"inserted": 2}

    monkeypatch.setattr(news_store, "collect", fake_collect)
    out = daemon._news_body(deps)
    assert seen == ["KR", "US"]
    assert out["KR"] == {"inserted": 2} and "error" in out["US"]
    assert out["pruned"] == 1 and news_store.count(conn) == 1
    # 큐 kind 로도 같은 본체
    jobq.enqueue(conn, "news_collect", {}, source="web", now=NOW)
    daemon._fast_queue_tick(deps, None)  # 가벼운 잡은 빠른 레인만 꺼낸다
    rows = [r for r in jobq.list_recent(conn) if r["kind"] == "news_collect"]
    assert rows[0]["status"] == "ok"
    assert seen == ["KR", "US", "KR", "US"]


def test_news_body_skipped_when_disabled(conn):
    deps = _deps(conn, FakeMarket(), now=lambda: NOW)
    deps.sources_cfg = SourcesConfig.model_validate(
        {"edgar": {"user_agent": "t t@e.com"}, "news": {"enabled": False}}
    )
    assert daemon._news_body(deps) == {"skipped": "news.enabled=false"}


def test_news_body_ingests_saveticker_folder(conn, monkeypatch, project):
    import shutil

    from helpers_sources import FIXTURES

    shutil.copytree(FIXTURES / "saveticker", project / "state" / "external" / "saveticker")
    deps = _deps(conn, FakeMarket(), now=lambda: NOW)
    deps.sources_cfg = _cfg()
    monkeypatch.setattr(news_store, "collect", lambda *a, **k: {"inserted": 0})
    out = daemon._news_body(deps)
    assert out["saveticker"]["inserted"] > 0 and news_store.count(conn) > 0
    assert daemon._news_body(deps)["saveticker"]["inserted"] == 0


# --- 장중 뉴스 발굴(Phase 23) -------------------------------------------------------


def _news(conn, items, *, market="KR", names=None, received=None):
    rows = []
    for i, (title, tickers) in enumerate(items):
        rows.append(
            {
                "title": title,
                "url": f"https://x/{abs(hash(title)) % 10**8}/{i}",
                "published_at": (received or NOW).isoformat(),
                "received_at": (received or NOW).isoformat(),
                "tickers": tickers,
            }
        )
    news_store.insert_items(
        conn,
        rows,
        market=market,
        source="t",
        query="q",
        lang="ko",
        names=names or {},
        extra_tickers=True,
    )


def _trigger_deps(conn, *, now=None):
    from trader.config import Universe

    deps = _deps(conn, FakeMarket(), now=now or (lambda: NOW))  # 10:00 KST = 장중
    deps.sources_cfg = _cfg()
    deps.universe = Universe(kr=["005930", "000660"])
    return deps


def _queued(conn):
    return [
        (r["kind"], r["params"], r["source"])
        for r in jobq.list_recent(conn)
        if r["status"] == "queued"
    ]


def test_news_trigger_enqueues_analyze_and_discover_for_novel_news(conn, project):
    deps = _trigger_deps(conn)
    _news(conn, [("삼성전자 대형 수주", ["005930"]), ("금리 동결", [])])
    out = daemon._news_trigger(conn=conn, deps=deps, market="KR", universe=deps.universe)
    assert out["novel"] == 2 and len(out["queued"]) == 1
    q = _queued(conn)
    kinds = sorted(k for k, _, _ in q)
    assert kinds == ["analyze_ticker", "discover"]
    an = next(p for k, p, s in q if k == "analyze_ticker")
    assert an["ticker"] == "005930" and an["dry_run"] is False
    assert all(s == "news" for _, _, s in q)
    assert (
        int(conn.execute("SELECT COUNT(*) FROM job_requests WHERE priority=5").fetchone()[0]) == 1
    )
    from trader.control import control

    assert control.get_flag(conn, "news_trigger_at:KR") == NOW.isoformat()
    assert control.get_flag(conn, "discover_count:KR:2026-10-24") == 1


def test_news_trigger_no_session_when_only_similar_news(conn, project):
    deps = _trigger_deps(conn)
    _news(conn, [("삼성전자 대형 수주", ["005930"])], received=NOW - timedelta(hours=3))
    daemon._news_trigger(conn=conn, deps=deps, market="KR", universe=deps.universe)
    before = len(_queued(conn))
    # 같은 사건의 다른 매체 기사만 새로 들어옴 → novel 0 → 추가 세션 없음
    _news(conn, [("삼성전자, 대형 수주 - 매체B", ["005930"])])
    later = NOW + timedelta(minutes=30)
    deps.now = lambda: later
    out = daemon._news_trigger(conn=conn, deps=deps, market="KR", universe=deps.universe)
    assert out["novel"] == 0 and out["queued"] == [] and out["discover"] is None
    assert len(_queued(conn)) == before
    assert conn.execute("SELECT novel FROM news_items WHERE title LIKE '%매체B'").fetchone()[0] == 0


def test_news_trigger_reevaluates_when_today_decision_exists(conn, project):
    deps = _trigger_deps(conn)
    conn.execute(
        "INSERT INTO decisions(date, market, ticker, action, json, created_at) "
        "VALUES ('2026-10-24','KR','005930','BUY','{}','2026-10-24T00:00:00+00:00')"
    )
    _news(conn, [("삼성전자 실적 발표", ["005930"])])
    out = daemon._news_trigger(conn=conn, deps=deps, market="KR", universe=deps.universe)
    q = _queued(conn)
    re = next(p for k, p, _ in q if k == "reevaluate")
    assert re["ticker"] == "005930" and out["queued"][0]["ticker"] == "005930"
    trig = conn.execute("SELECT kind, detail, price FROM triggers").fetchone()
    assert trig["kind"] == "news" and trig["price"] is None
    assert "삼성전자 실적 발표" in trig["detail"]


def test_news_trigger_skips_outside_window_or_paused_but_marks_novel(conn, project):
    from trader.control import control

    deps = _trigger_deps(conn)
    control.set_flag(conn, "paused", True)
    _news(conn, [("장중 기사 둘", ["000660"])])
    out = daemon._news_trigger(conn=conn, deps=deps, market="KR", universe=deps.universe)
    assert out["novel"] == 1 and out["skipped"] == "일시정지" and _queued(conn) == []
    assert conn.execute("SELECT novel FROM news_items").fetchone()[0] == 1
    control.set_flag(conn, "paused", False)
    deps.settings.discovery.intraday.enabled = False
    later = NOW + timedelta(minutes=30)
    deps.now = lambda: later
    _news(conn, [("SK하이닉스 공장 화재", ["000660"])], received=later)
    assert (
        "enabled"
        in daemon._news_trigger(conn=conn, deps=deps, market="KR", universe=deps.universe)[
            "skipped"
        ]
    )
    deps.settings.discovery.intraday.enabled = True
    night = datetime(2026, 10, 24, 14, 0, tzinfo=UTC)  # 23:00 KST
    deps.now = lambda: night
    _news(conn, [("야간 기사", ["005930"])], received=night)
    out3 = daemon._news_trigger(conn=conn, deps=deps, market="KR", universe=deps.universe)
    assert out3["novel"] == 1 and "장중 창" in out3["skipped"] and _queued(conn) == []


def test_news_trigger_respects_discover_cap(conn, project):
    from trader.control import control

    deps = _trigger_deps(conn)
    deps.settings.discovery.intraday.discover_cap = 1
    control.set_flag(conn, "discover_count:KR:2026-10-24", 1)
    _news(conn, [("금리 동결", [])])
    out = daemon._news_trigger(conn=conn, deps=deps, market="KR", universe=deps.universe)
    assert out["discover"] == "상한 1" and _queued(conn) == []


def test_discover_body_runs_fake_scout_and_queues_candidates(conn, project, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    (project / "config" / "universe.yaml").write_text(
        "kr: ['005930', '000660']\n", encoding="utf-8"
    )
    deps = _trigger_deps(conn)
    deps.state = project / "state"
    _news(conn, [("삼성전자 대형 수주", ["005930"])])
    daemon._news_trigger(conn=conn, deps=deps, market="KR", universe=deps.universe)
    # analyze_ticker(priority 5)가 먼저 잡히므로 취소하고 discover 만 데몬이 실행
    an = next(r for r in jobq.list_recent(conn) if r["kind"] == "analyze_ticker")
    assert jobq.cancel(conn, an["id"], now=NOW)
    daemon._queue_tick(deps, None)
    rows = {r["kind"]: r for r in jobq.list_recent(conn)}
    assert rows["discover"]["status"] == "ok", rows["discover"]
    res = rows["discover"]["result"]
    assert res["novel"] == 1 and res["candidates"] == 1
    # 발굴 후보 005930 → 재분석 요청(취소된 앞 요청은 중복이 아니다)
    assert res["queued"][0]["ticker"] == "005930" and res["queued"][0]["tier"] == "universe"
    from trader.orchestrator.run_store import market_dir, read_checkpoint

    d = market_dir(daemon.run_store.run_date_for("KR", NOW), "KR", deps.state)
    assert read_checkpoint(d, "_discovery_r1")["data"]["candidates"][0]["ticker"] == "005930"


def test_news_trigger_held_long_horizon_needs_three_novel_items(conn, project):
    """Phase 23 T: 보유 종목은 프로필 news_min_novel(장기 3건)을 넘어야 재분석 요청."""
    from decimal import Decimal

    from trader.broker.fake import FakeBroker
    from trader.broker.models import Position

    deps = _trigger_deps(conn)
    pos = Position(
        ticker="005930",
        market="KR",
        qty=1,
        avg_price=Decimal("70000"),
        current_price=Decimal("71000"),
        eval_amount=Decimal("71000"),
        pnl_pct=1.4,
    )
    deps.brokers = {"paper-main": FakeBroker(deps.accounts[0], positions=[pos])}
    conn.execute(
        "INSERT INTO decisions(date, market, ticker, action, json, created_at) "
        "VALUES ('2026-10-24','KR','005930','BUY','{\"horizon_days\": 60}',"
        "'2026-10-24T00:00:00+00:00')"
    )
    _news(conn, [("삼성전자 신규 수주", ["005930"])])
    out = daemon._news_trigger(conn=conn, deps=deps, market="KR", universe=deps.universe)
    assert out["queued"] == [] and out["below_profile"] == [
        {"ticker": "005930", "news": 1, "need": 3}
    ]
    assert [k for k, _, _ in _queued(conn)] == ["discover"]
