import json
from datetime import UTC, datetime

import pytest

from trader.saveticker import api_contract, native


def view(stream):
    return next(
        v
        for v in native.build_plan(datetime(2026, 10, 30, tzinfo=UTC))["views"]
        if v["stream"] == stream
    )


def envelope(v, data):
    return {
        "url": api_contract.descriptor(v)["url"],
        "status": 200,
        "content_type": "application/json",
        "data": data,
    }


def test_exact_contracts_and_unknown_conditions():
    assert api_contract.descriptor(view("news_all"))["url"].endswith(
        "page=1&page_size=20&sort=created_at_desc&label_group=1&label_name=1"
    )
    assert api_contract.descriptor(view("calendar_2026-10"))["url"].endswith(
        "start_date=2026-09-30&end_date=2026-11-01"
    )
    assert api_contract.descriptor(view("market_gain_regular")) is None
    assert api_contract.descriptor(view("community_gain_live")) is None
    assert api_contract.descriptor(view("news_reports"))["url"].endswith(
        "/api/report/list?page=1&page_size=20"
    )


def test_calendar_filters_padding_and_rejects_malformed_empty():
    v = view("calendar_2026-10")
    data = {
        "events": [
            {"id": "1", "title": "meeting", "event_date": "2026-10-02", "event_date_only": True},
            {"id": "2", "title": "padding", "event_date": "2026-09-30", "event_date_only": True},
        ],
        "total_count": 2,
    }
    items = api_contract.parse_snapshot(v, envelope(v, data))
    assert len(items) == 1 and items[0].title == "meeting"
    with pytest.raises(ValueError):
        api_contract.parse_snapshot(v, envelope(v, {}))
    assert api_contract.parse_snapshot(v, envelope(v, {"events": [], "total_count": 0})) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("url", "https://evil.invalid/api/news/list"),
        ("status", 403),
        ("content_type", "text/html"),
        ("url", "https://www.saveticker.com/api/news/list?page=1&page=2"),
    ],
)
def test_response_provenance(field, value):
    v = view("news_all")
    message = envelope(v, {"news_list": []})
    message[field] = value
    with pytest.raises(ValueError):
        api_contract.parse_snapshot(v, message)


def test_index_is_exact_five_and_regular_not_fallback():
    v = view("community_index_regular")
    rows = [
        {"symbol": s, "labelKo": s, "price": 100, "regular": {"price": 90, "changePercent": 1}}
        for s in ["QQQ", "SPY", "DIA", "IWM", "SOXX", "OTHER"]
    ]
    items = api_contract.parse_snapshot(v, envelope(v, {"items": rows}))
    assert [x.tickers[0] for x in items] == ["QQQ", "SPY", "DIA", "IWM", "SOXX"]
    assert all(x.price == "90" and x.basis == "regular" for x in items)
    del rows[0]["regular"]
    with pytest.raises(ValueError):
        api_contract.parse_snapshot(v, envelope(v, {"items": rows}))


def test_native_api_job_is_separate_and_single_use(tmp_path):
    client = native.NativeSession(tmp_path)
    job = client.handle({"id": 1, "type": "api_begin", "stream": "news_all"})
    assert job["ok"] and job["request"]["method"] == "GET"
    message = {
        "id": 2,
        "type": "api_snapshot",
        "stream": "news_all",
        "job_id": job["job_id"],
        "response": {
            "url": job["request"]["url"],
            "status": 200,
            "content_type": "application/json",
            "data": {
                "news_list": [
                    {"id": "1", "title": "test", "source": "save-pick", "author_secret": "never"}
                ]
            },
        },
    }
    forged = {**message, "job_id": "wrong"}
    assert not client.handle(forged)["ok"]
    assert (
        client.handle(
            {
                **message,
                "type": "snapshot",
                "page_url": "https://www.saveticker.com/news",
                "html": "",
                "selection": {"loaded": True, "source": "전체", "category": "전체"},
            }
        )["ok"]
        is False
    )
    result = client.handle(message)
    assert result["ok"] and result["status"] == "partial"
    latest = json.loads((tmp_path / "data/latest/news_all.json").read_text())
    assert latest["items"][0]["title"] == "test"
    assert "never" not in json.dumps(latest)
    assert not client.handle(message)["ok"]


def test_unsupported_does_not_allocate_job_and_failure_keeps_data(tmp_path):
    client = native.NativeSession(tmp_path)
    bad = client.handle({"id": 1, "type": "api_begin", "stream": "market_gain_regular"})
    assert bad["error"] == "api_contract_unverified" and not client._jobs
    job = client.handle({"id": 2, "type": "api_begin", "stream": "news_all"})
    cancel = {"id": 3, "type": "api_cancel", "stream": "news_all", "job_id": job["job_id"]}
    assert client.handle(cancel)["ok"]
    assert not (tmp_path / "data/latest/news_all.json").exists()
    assert not client._jobs


@pytest.mark.parametrize(
    "stream,data",
    [
        ("news_all", {"news_list": [], "total_count": 20}),
        ("news_reports", {"reports": [], "total_count": 20, "page": 1, "page_size": 20}),
        (
            "calendar_2026-10",
            {"events": [{"id": "1", "title": "bad", "event_date": "invalid"}], "total_count": 1},
        ),
        (
            "calendar_2026-10",
            {
                "events": [
                    {"id": "1", "title": "out of requested range", "event_date": "2026-08-01"}
                ],
                "total_count": 1,
            },
        ),
    ],
)
def test_malformed_or_inconsistent_empty_must_not_replace_latest(stream, data):
    v = view(stream)
    with pytest.raises(ValueError):
        api_contract.parse_snapshot(v, envelope(v, data))
