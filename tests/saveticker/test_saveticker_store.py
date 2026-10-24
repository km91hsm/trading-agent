import json
from datetime import UTC, datetime

import pytest


def capture(items, *, when="2026-10-25T00:00:00+00:00", stream="news_all", **kwargs):
    from trader.sources.saveticker.models import Capture

    return Capture(
        stream=stream,
        page_url="https://www.saveticker.com/news",
        observed_at=when,
        items=items,
        **kwargs,
    )


def test_revision_dedup_survives_restart_and_exports(tmp_path):
    from trader.sources.saveticker.store import Store

    root = tmp_path / "feed"
    first = {"id": "n1", "title": "AI 투자", "body": "처음", "tickers": ["NVDA"]}
    assert Store(root).save(capture([first]))["changed"] == 1
    assert Store(root).save(capture([first], when="2026-10-25T00:01:00+00:00"))["changed"] == 0
    assert (
        Store(root).save(capture([first | {"body": "정정"}], when="2026-10-25T00:02:00+00:00"))[
            "changed"
        ]
        == 1
    )
    rows = [json.loads(s) for s in (root / "2026-10-25" / "news.jsonl").read_text().splitlines()]
    assert [r["item"]["body"] for r in rows] == ["처음", "정정"]
    assert json.loads((root / "latest" / "news_all.json").read_text())["items"][0]["body"] == "정정"


def test_failed_capture_keeps_previous_success_and_marks_status(tmp_path):
    from trader.sources.saveticker.store import Store

    store = Store(tmp_path)
    store.save(capture([{"id": "n1", "title": "기사"}]))
    store.save(
        capture(
            [], status="auth_required", reason="login_required", when="2026-10-25T00:01:00+00:00"
        )
    )
    latest = json.loads((tmp_path / "latest" / "news_all.json").read_text())
    assert len(latest["items"]) == 1
    status = store.status()["streams"]["news_all"]
    assert status["status"] == "auth_required"
    assert status["last_success_at"] == "2026-10-25T00:00:00+00:00"
    assert status["last_attempt_at"] == "2026-10-25T00:01:00+00:00"


def test_query_is_point_in_time_including_revision_and_exact_ticker(tmp_path):
    from trader.sources.saveticker.store import Store

    store = Store(tmp_path)
    item = {"id": "a", "title": "AAPL 전망", "body": "old", "tickers": ["AAPL"]}
    store.save(capture([item]))
    store.save(capture([item | {"body": "new"}], when="2026-10-25T00:02:00+00:00"))
    rows = store.query(asof=datetime(2026, 10, 25, 0, 1, tzinfo=UTC), ticker="AAPL")
    assert [r["item"]["body"] for r in rows] == ["old"]
    assert store.query(asof=datetime(2026, 10, 25, 0, 1, tzinfo=UTC), ticker="AAP") == []
    assert store.query(asof=datetime(2026, 10, 24, tzinfo=UTC)) == []


def test_published_future_rejected_but_scheduled_future_allowed(tmp_path):
    from trader.sources.saveticker.store import Store

    store = Store(tmp_path)
    store.save(
        capture([{"id": "future", "title": "미래", "published_at": "2026-10-26T00:00:00+00:00"}])
    )
    store.save(
        capture(
            [{"id": "event", "title": "금리 결정", "scheduled_at": "2026-10-29T00:00:00+00:00"}],
            stream="calendar_2026-10",
        )
    )
    rows = store.query(asof=datetime(2026, 10, 25, 0, 1, tzinfo=UTC))
    assert [r["item"]["id"] for r in rows] == ["event"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"stream": "../escape"},
        {"observed_at": "2026-10-25T00:00:00"},
        {"page_url": "https://evil.example/news"},
        {"items": [{"id": "x", "title": "a", "password": "secret"}]},
    ],
)
def test_capture_rejects_unsafe_data(kwargs):
    from pydantic import ValidationError

    from trader.sources.saveticker.models import Capture

    base = dict(
        stream="news_all",
        page_url="https://www.saveticker.com/news",
        observed_at="2026-10-25T00:00:00Z",
        items=[],
    )
    with pytest.raises(ValidationError):
        Capture(**(base | kwargs))


def test_relative_display_time_does_not_create_revision(tmp_path):
    from trader.sources.saveticker.store import Store

    store = Store(tmp_path)
    item = {"id": "a", "title": "속보", "displayed_time": "1분 전"}
    store.save(capture([item]))
    assert (
        store.save(capture([item | {"displayed_time": "2분 전"}], when="2026-10-25T00:01:00Z"))[
            "changed"
        ]
        == 0
    )


def test_partial_is_not_complete_and_empty_ok_is_rejected(tmp_path):
    from pydantic import ValidationError

    from trader.sources.saveticker.store import Store

    store = Store(tmp_path)
    store.save(capture([{"id": "a", "title": "기사"}], status="partial", reason="scroll_limit"))
    assert store.status()["streams"]["news_all"]["status"] == "partial"
    with pytest.raises(ValidationError):
        capture([])


def test_empty_partial_does_not_replace_last_useful_snapshot(tmp_path):
    from trader.sources.saveticker.store import Store

    store = Store(tmp_path)
    store.save(capture([{"id": "a", "title": "기존 기사"}]))
    store.save(
        capture([], status="partial", reason="empty_or_unavailable", when="2026-10-25T00:01:00Z")
    )
    latest = json.loads((tmp_path / "latest" / "news_all.json").read_text())
    assert latest["items"][0]["title"] == "기존 기사"
    assert store.status()["streams"]["news_all"]["last_success_at"] == "2026-10-25T00:00:00+00:00"


def test_query_collapses_same_news_in_multiple_source_tabs(tmp_path):
    from trader.sources.saveticker.store import Store

    store = Store(tmp_path)
    item = {"id": "a", "title": "같은 기사", "kind": "news"}
    store.save(capture([item], stream="news_all"))
    store.save(capture([item], stream="news_reuters", when="2026-10-25T00:01:00Z"))
    assert len(store.query(asof=datetime(2026, 10, 25, 0, 2, tzinfo=UTC))) == 1


def test_new_capture_does_not_rewrite_previous_day_jsonl(tmp_path):
    from trader.sources.saveticker.store import Store

    store = Store(tmp_path)
    store.save(capture([{"id": "a", "title": "전날 기사"}]))
    old = tmp_path / "2026-10-25" / "news.jsonl"
    stamp = old.stat().st_mtime_ns
    store.save(capture([{"id": "b", "title": "새 기사"}], when="2026-10-26T00:00:00Z"))
    assert old.stat().st_mtime_ns == stamp


def test_interrupted_jsonl_export_recovers_from_sqlite(tmp_path):
    from trader.sources.saveticker.store import Store

    store = Store(tmp_path)
    store.save(capture([{"id": "a", "title": "첫 기사"}]))
    p = tmp_path / "2026-10-25" / "news.jsonl"
    with p.open("a") as f:
        f.write('{"interrupted')
    store.save(capture([{"id": "b", "title": "다음 기사"}], when="2026-10-25T00:01:00Z"))
    rows = [json.loads(line) for line in p.read_text().splitlines()]
    assert [r["item"]["title"] for r in rows] == ["첫 기사", "다음 기사"]


def test_newer_pending_tab_does_not_hide_known_body_at_cutoff(tmp_path):
    from trader.sources.saveticker.store import Store

    store = Store(tmp_path)
    item = {
        "id": "a",
        "title": "기사",
        "kind": "news",
        "body": "확인된 본문",
        "detail_status": "ok",
    }
    store.save(capture([item], stream="news_all"))
    store.save(
        capture(
            [item | {"body": "", "detail_status": "pending"}],
            stream="news_reuters",
            when="2026-10-25T00:01:00Z",
        )
    )
    rows = store.query(asof=datetime(2026, 10, 25, 0, 2, tzinfo=UTC), limit=1)
    assert len(rows) == 1 and rows[0]["item"]["body"] == "확인된 본문"
    assert rows[0]["received_at"] == "2026-10-25T00:00:00+00:00"


def test_same_stream_partial_refresh_retains_last_known_detail_in_query(tmp_path):
    from trader.sources.saveticker.store import Store

    store = Store(tmp_path)
    item = {
        "id": "a",
        "title": "기사",
        "kind": "news",
        "body": "확인된 본문",
        "detail_status": "ok",
    }
    store.save(capture([item]))
    store.save(
        capture([item | {"body": "", "detail_status": "pending"}], when="2026-10-25T00:01:00Z")
    )
    rows = store.query(asof=datetime(2026, 10, 25, 0, 2, tzinfo=UTC))
    assert rows[0]["item"]["body"] == "확인된 본문"


def test_query_rankings_respect_current_membership_at_asof(tmp_path):
    from trader.sources.saveticker.store import Store

    store = Store(tmp_path)
    store.save(
        capture([{"id": "a", "title": "순위 이탈", "kind": "quote"}], stream="market_gain_live")
    )
    store.save(
        capture(
            [{"id": "b", "title": "신규 진입", "kind": "quote"}],
            stream="market_gain_live",
            when="2026-10-25T00:02:00Z",
        )
    )
    assert [
        r["item"]["id"] for r in store.query(asof=datetime(2026, 10, 25, 0, 1, tzinfo=UTC))
    ] == ["a"]
    assert [
        r["item"]["id"] for r in store.query(asof=datetime(2026, 10, 25, 0, 3, tzinfo=UTC))
    ] == ["b"]


def test_stream_labels_survive_export_and_query(tmp_path):
    from trader.sources.saveticker.store import Store

    store = Store(tmp_path)
    c = capture([{"id": "a", "title": "기사"}], view={"source": "로이터", "category": "분석"})
    store.save(c)
    row = json.loads((tmp_path / "2026-10-25" / "news.jsonl").read_text().splitlines()[0])
    assert row["view"]["source"] == "로이터"
    assert store.status()["streams"]["news_all"]["view"]["category"] == "분석"


def test_store_upgrades_existing_probe_database_without_dropping_history(tmp_path):
    import sqlite3

    from trader.sources.saveticker.store import Store

    with sqlite3.connect(tmp_path / "feed.sqlite3") as db:
        db.execute(
            "CREATE TABLE revisions (seq INTEGER PRIMARY KEY,stream TEXT,item_id TEXT,"
            "received_at TEXT,digest TEXT,payload TEXT)"
        )
        db.execute(
            "INSERT INTO revisions VALUES(1,?,?,?,?,?)",
            (
                "news_all",
                "old",
                "2026-10-24T00:00:00+00:00",
                "x",
                json.dumps({"id": "old", "title": "이전 기사", "kind": "news"}),
            ),
        )
    store = Store(tmp_path)
    store.save(capture([{"id": "new", "title": "새 기사"}]))
    assert (
        json.loads((tmp_path / "2026-10-24" / "news.jsonl").read_text())["item"]["title"]
        == "이전 기사"
    )
    assert len(store.query(asof=datetime(2026, 10, 25, 0, 1, tzinfo=UTC))) == 2


def test_store_refuses_newer_database_schema_without_creating_tables(tmp_path):
    import sqlite3

    from trader.sources.saveticker.store import Store

    with sqlite3.connect(tmp_path / "feed.sqlite3") as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError):
        Store(tmp_path)
    with sqlite3.connect(tmp_path / "feed.sqlite3") as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
