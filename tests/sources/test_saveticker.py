"""세이브티커 로컬 폴더 리더(sources/saveticker): 뉴스 적재·시장 분리·발행시각·스냅샷 asof·
종목 언급."""

import shutil
from datetime import UTC, datetime
from pathlib import Path

from helpers_sources import FIXTURES

from trader.sources import news_store, saveticker

SAMPLE = FIXTURES / "saveticker"
T_AFTER = datetime(2026, 10, 25, 3, 0, tzinfo=UTC)  # 샘플 관측 뒤
T_BEFORE = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)  # 샘플 관측 전


def _root(tmp_path: Path) -> Path:
    dst = tmp_path / "saveticker"
    shutil.copytree(SAMPLE, dst)
    return dst


def test_resolve_dir_and_available(project):
    assert saveticker.resolve_dir(None) is None
    p = saveticker.resolve_dir("state/external/saveticker")
    assert p == project / "state" / "external" / "saveticker" and not saveticker.available(p)
    assert saveticker.snapshot(p, asof=T_AFTER) is None
    assert saveticker.available(SAMPLE)


def test_published_at_and_markets():
    assert (
        saveticker.published_at_of({"published_at": "2026-10-24T23:30:00+00:00"})
        == "2026-10-24T23:30:00+00:00"
    )
    assert (
        saveticker.published_at_of({"displayed_time": "2026. 10. 24. 07:40"})
        == "2026-10-23T22:40:00+00:00"
    )
    assert saveticker.published_at_of({"displayed_time": "2시간 전"}) is None
    assert saveticker.markets_of({"tickers": []}) == ["KR", "US"]
    assert saveticker.markets_of({"tickers": ["005930"]}) == ["KR"]
    assert saveticker.markets_of({"tickers": ["GSK"]}) == ["US"]
    assert saveticker.markets_of({"tickers": ["005930", "AAPL"]}) == ["KR", "US"]


def test_read_news_keeps_last_revision_and_report_excerpt(tmp_path):
    root = _root(tmp_path)
    items = saveticker.read_news_items(root, today=T_AFTER.date())
    ids = {i["id"] for i in items}
    assert len(items) == len(ids) and any(i["_stream"] == "news_reports" for i in items)
    report = next(i for i in items if i["_stream"] == "news_reports")
    st = saveticker.to_store_item(root, report)
    assert st["title"].startswith("SAVE 데일리 리포트 2026-10-23") and "S&P500" in st["summary"]
    assert st["published_at"] == "2026-10-22T23:00:00+00:00" and st["url"].startswith(
        "https://www.saveticker.com/news#"
    )
    news = next(i for i in items if i["_stream"] == "news_all" and i.get("tickers"))
    st2 = saveticker.to_store_item(root, news)
    assert st2["tickers"] and st2["publisher"] and st2["received_at"]


def test_ingest_news_is_idempotent_and_splits_markets(conn, tmp_path):
    root = _root(tmp_path)
    r = saveticker.ingest_news(conn, root, now=lambda: T_AFTER)
    assert r["items"] > 0 and r["inserted"] > r["items"]  # 티커 없는 거시 뉴스는 KR·US 두 행
    assert set(r["by_market"]) == {"KR", "US"}
    assert saveticker.ingest_news(conn, root, now=lambda: T_AFTER)["inserted"] == 0
    us = news_store.recent(conn, "US", asof=T_AFTER, hours=24 * 30, limit=200)
    assert any(i["source"].startswith("saveticker:") for i in us)
    gsk = news_store.recent(conn, "US", asof=T_AFTER, hours=24 * 30, ticker="GSK")
    assert gsk and all("GSK" in i["tickers"] for i in gsk)
    # 세이브티커 티커가 붙은 US 뉴스는 KR 행이 없다
    assert not any(
        "GSK" in i["tickers"]
        for i in news_store.recent(conn, "KR", asof=T_AFTER, hours=24 * 30, limit=200)
    )
    # 폴더가 없으면 skipped
    assert saveticker.ingest_news(conn, tmp_path / "none", now=lambda: T_AFTER) == {
        "skipped": "saveticker 폴더 없음"
    }


def test_snapshot_filters_by_asof_and_collects_streams(tmp_path):
    root = _root(tmp_path)
    snap = saveticker.snapshot(root, asof=T_AFTER)
    assert snap["trends"][0]["keyword"] == "트럼프" and snap["trends"][0]["rank"] == 1
    assert {r["tickers"][0] for r in snap["index"]} >= {"QQQ", "SPY"}
    assert len(snap["movers"]["gain"]) == 5 and snap["movers"]["turnover"][0]["turnover"]
    assert snap["community_top5"] and snap["report"]["report_date"] == "2026-10-23"
    assert "S&P500" in snap["report"]["excerpt"] and len(snap["report"]["excerpt"]) <= 1500
    assert all("2026-10-24" <= c["scheduled_at"][:10] <= "2026-11-01" for c in snap["calendar"])
    assert snap["streams_status"]["news_all"] == "partial" and snap["observed_at"]
    # 관측 전 asof 면 화면이 전부 비어 있다(룩어헤드)
    early = saveticker.snapshot(root, asof=T_BEFORE)
    assert early["trends"] == [] and early["movers"]["gain"] == [] and early["report"] is None


def test_for_ticker_mentions_and_market_context(tmp_path):
    snap = saveticker.snapshot(_root(tmp_path), asof=T_AFTER)
    qqq = saveticker.for_ticker(snap, "qqq")
    assert "community_top5" in qqq["mentions"] and qqq["trends"]
    none = saveticker.for_ticker(snap, "005930")
    assert none["mentions"] == {} and none["index"]
    assert saveticker.for_ticker(None, "QQQ") is None
