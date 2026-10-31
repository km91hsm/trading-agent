import json

import pytest

from trader.saveticker import api_contract, native


def send(client, kind, **kwargs):
    return client.handle({"id": 1, "type": kind, **kwargs})


def response(url, data):
    return {"url": url, "status": 200, "content_type": "application/json", "data": data}


def setup(client):
    job = send(client, "api_context_begin")
    assert job["ok"]
    result = send(
        client,
        "api_context",
        job_id=job["job_id"],
        response=response(
            job["request"]["url"],
            {"groups": [{"groupId": 42, "isDefault": True, "name": "must not be persisted"}]},
        ),
    )
    assert result["ok"]
    return result


def test_default_group_is_projected_and_bound_to_rank_requests(tmp_path):
    client = native.NativeSession(tmp_path)
    assert not send(client, "api_begin", stream="market_gain_live")["ok"]
    setup(client)
    job = send(client, "api_begin", stream="market_gain_live")
    assert job["ok"]
    assert job["request"]["url"].endswith("tab=gainers&groupId=42&excludeRisky=true")
    assert not list(tmp_path.rglob("*groups*.json"))


@pytest.mark.parametrize(
    "groups",
    [
        [],
        [{"groupId": 1, "isDefault": True}, {"groupId": 2, "isDefault": True}],
        [{"groupId": "1&private=true", "isDefault": True}],
        [{"groupId": True, "isDefault": True}],
    ],
)
def test_ambiguous_or_invalid_group_does_not_enable_requests(tmp_path, groups):
    client = native.NativeSession(tmp_path)
    job = send(client, "api_context_begin")
    assert job["ok"]
    assert not send(
        client,
        "api_context",
        job_id=job["job_id"],
        response=response(job["request"]["url"], {"groups": groups}),
    )["ok"]
    assert not send(client, "api_begin", stream="market_gain_live")["ok"]


def test_news_detail_is_native_issued_correlated_and_saved(tmp_path):
    client = native.NativeSession(tmp_path)
    job = send(client, "api_begin", stream="news_all")
    base = response(
        job["request"]["url"],
        {"news_list": [{"id": "news_one", "title": "One", "source": "save-pick"}]},
    )
    prepared = send(client, "api_prepare", stream="news_all", job_id=job["job_id"], response=base)
    assert prepared["ok"]
    child = prepared["requests"][0]
    assert child["request"]["url"] == "https://www.saveticker.com/api/news/detail?id=news_one"
    detail = response(
        child["request"]["url"],
        {
            "id": "news_one",
            "title": "One",
            "source": "save-pick",
            "content": [{"type": "text", "content": "Full body"}],
        },
    )
    forged = {**detail, "data": {**detail["data"], "id": "news_other"}}
    assert not send(
        client,
        "api_snapshot",
        stream="news_all",
        job_id=job["job_id"],
        response=base,
        details=[forged],
    )["ok"]
    saved = send(
        client,
        "api_snapshot",
        stream="news_all",
        job_id=job["job_id"],
        response=base,
        details=[detail],
    )
    assert saved["ok"]
    item = json.loads((tmp_path / "data/latest/news_all.json").read_text())["items"][0]
    assert item["body"] == "Full body" and item["detail_status"] == "ok"
    job = send(client, "api_begin", stream="news_all")
    prepared = send(client, "api_prepare", stream="news_all", job_id=job["job_id"], response=base)
    assert prepared["requests"] == [], "cached complete body is not fetched every cycle"


def test_prepared_base_cannot_be_swapped(tmp_path):
    client = native.NativeSession(tmp_path)
    job = send(client, "api_begin", stream="news_all")
    base = response(job["request"]["url"], {"news_list": [{"id": "one", "title": "One"}]})
    assert send(client, "api_prepare", stream="news_all", job_id=job["job_id"], response=base)["ok"]
    changed = {**base, "data": {"news_list": [{"id": "two", "title": "Two"}]}}
    assert not send(
        client,
        "api_snapshot",
        stream="news_all",
        job_id=job["job_id"],
        response=changed,
        details=[],
    )["ok"]
    assert not (tmp_path / "data/latest/news_all.json").exists()


def test_report_document_is_bound_to_detail_and_existing_pdf_ticket(tmp_path):
    client = native.NativeSession(tmp_path)
    job = send(client, "api_begin", stream="news_reports")
    base = response(
        job["request"]["url"],
        {
            "reports": [{"id": "7", "title": "2026년 10월 30일 (금)", "has_pdf": True}],
            "page": 1,
            "page_size": 20,
            "total_count": 1,
        },
    )
    prepared = send(
        client, "api_prepare", stream="news_reports", job_id=job["job_id"], response=base
    )
    assert prepared["ok"]
    child = prepared["requests"][0]
    assert child["request"]["url"].endswith("/api/report/detail?id=7")
    detail = response(
        child["request"]["url"],
        {
            "report": {
                "id": "7",
                "title": "2026년 10월 30일 (금)",
                "pdf_url": "/api/uploads/documents/77",
            }
        },
    )
    saved = send(
        client,
        "api_snapshot",
        stream="news_reports",
        job_id=job["job_id"],
        response=base,
        details=[detail],
    )
    assert saved["ok"] and len(saved["pdf_items"]) == 1
    ticket = send(client, "api_pdf_begin", item_id=saved["pdf_items"][0]["id"])
    assert ticket["ok"] and ticket["ticket"] in client._tickets
    assert ticket["request"]["url"].endswith(
        "/api/report/download?url=%2Fapi%2Fuploads%2Fdocuments%2F77"
    )
    assert ticket["download_name"].endswith(".pdf")
    assert not send(client, "api_pdf_begin", item_id="unrelated")["ok"]


def test_live_snapshot_never_substitutes_for_explicit_regular_basis(tmp_path):
    client = native.NativeSession(tmp_path)
    setup(client)
    job = send(client, "api_begin", stream="market_gain_regular")
    assert job["ok"]
    data = {
        "tab": "gainers",
        "basis": "live",
        "session": "open",
        "excludeRisky": True,
        "items": [{"symbol": "AAA", "rank": 1, "price": 12, "changePercent": 3}],
    }
    bad = send(
        client,
        "api_snapshot",
        stream="market_gain_regular",
        job_id=job["job_id"],
        response=response(job["request"]["url"], {**data, "session": "closed"}),
    )
    assert not bad["ok"]
    good = send(
        client,
        "api_snapshot",
        stream="market_gain_regular",
        job_id=job["job_id"],
        response=response(job["request"]["url"], data),
    )
    assert not good["ok"]
    assert not (tmp_path / "data/latest/market_gain_regular.json").exists()


def test_popular_requires_exact_server_tab_basis_filter(tmp_path):
    client = native.NativeSession(tmp_path)
    setup(client)
    v = next(v for v in client._plan["views"] if v["stream"] == "market_gain_live")
    request = api_contract.descriptor(v, group_id=42)
    data = {
        "tab": "losers",
        "basis": "live",
        "session": "open",
        "excludeRisky": True,
        "items": [{"symbol": "AAA", "rank": 1, "price": 12, "changePercent": 3}],
    }
    with pytest.raises(ValueError):
        api_contract.parse_snapshot(v, response(request["url"], data), group_id=42)


@pytest.mark.parametrize(
    "stream,tab",
    [
        ("community_gain_regular", "gainers"),
        ("community_loss_regular", "losers"),
        ("market_gain_regular", "gainers"),
        ("market_loss_regular", "losers"),
        ("market_turnover_regular", "turnover"),
    ],
)
def test_explicit_regular_request_and_server_basis(tmp_path, stream, tab):
    client = native.NativeSession(tmp_path)
    setup(client)
    job = send(client, "api_begin", stream=stream)
    suffix = f"tab={tab}&groupId=42&basis=regular"
    if tab != "turnover":
        suffix += "&excludeRisky=true"
    assert job["request"]["url"].endswith(suffix)
    data = {
        "tab": tab,
        "basis": "regular",
        "session": "open",
        "excludeRisky": tab != "turnover",
        "asOf": "2026-10-30T20:00:00Z",
        "items": [{"symbol": "AAA", "rank": 1, "price": 12, "changePercent": 3}],
    }
    saved = send(
        client,
        "api_snapshot",
        stream=stream,
        job_id=job["job_id"],
        response=response(job["request"]["url"], data),
    )
    assert saved["ok"] and saved["count"] == 1
    latest = json.loads((tmp_path / f"data/latest/{stream}.json").read_text())
    assert latest["items"][0]["basis"] == "regular"
    assert latest["view"]["requested_regular"] is True
