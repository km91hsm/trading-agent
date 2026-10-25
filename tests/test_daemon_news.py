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
    daemon._queue_tick(deps, None)
    assert jobq.list_recent(conn)[0]["status"] == "ok"
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
