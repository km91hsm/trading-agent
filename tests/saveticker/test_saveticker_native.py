import io
import json
import struct
from datetime import UTC, datetime

from trader.saveticker import native

ORIGIN = "chrome-extension://gfchfnjghmjiidifohjglpodaakdphho/"
EXTENSION = "gfchfnjghmjiidifohjglpodaakdphho"


def session(tmp_path):
    return native.NativeSession(tmp_path)


def snapshot(client, stream="market_gain_live", **updates):
    job = client.handle({"id": 1, "type": "begin", "stream": stream})
    message = {
        "id": 2,
        "type": "snapshot",
        "stream": stream,
        "job_id": job["job_id"],
        "page_url": "https://www.saveticker.com/watchlist",
        "html": "",
        "selection": {"loaded": True, "tab": "상승", "regular": False},
    }
    message.update(updates)
    return message


def test_plan_preserves_all_lanes_and_five_months():
    plan = native.build_plan(datetime(2026, 12, 1, tzinfo=UTC))
    streams = {v["stream"] for v in plan["views"]}
    assert {
        "news_all",
        "news_reports",
        "community_top5",
        "community_index_regular",
        "market_turnover_live",
        "market_loss_regular",
    } <= streams
    assert {s for s in streams if s.startswith("calendar_")} == {
        "calendar_2026-11",
        "calendar_2026-12",
        "calendar_2027-01",
        "calendar_2027-02",
        "calendar_2027-03",
    }
    assert all(v["interval_s"] >= 30 for v in plan["views"])


def test_community_live_accepts_only_explicit_disabled_equivalence(tmp_path):
    client = session(tmp_path)
    message = snapshot(
        client,
        "community_index_live",
        page_url="https://www.saveticker.com/community",
        selection={
            "loaded": True,
            "tab": "지수",
            "regular": True,
            "regular_disabled": True,
            "regular_equivalent": True,
            "regular_equivalence_label": "지금은 실시간과 정규장 기준이 같아요",
        },
        html=(
            '<div role="button" data-clickable aria-label="QQQ 나스닥100">'
            "<span>QQQ</span><span>$100</span><span>+1%</span></div>"
        ),
    )
    message["selection"]["regular_equivalence_label"] = "다른 안내"
    assert client.handle(message)["error"] == "selection_mismatch"
    message["selection"]["regular_equivalence_label"] = "지금은 실시간과 정규장 기준이 같아요"
    message["selection"]["regular_disabled"] = False
    assert client.handle(message)["error"] == "selection_mismatch"
    message["selection"]["regular_disabled"] = True
    saved = client.handle(message)
    assert saved["ok"] and saved["count"] == 1
    data = json.loads((tmp_path / "data/latest/community_index_live.json").read_text())
    assert data["view"]["requested_regular"] is False
    assert "실시간과 정규장 기준이 같아요" in data["items"][0]["basis"]


def test_market_cannot_claim_community_regular_equivalence(tmp_path):
    client = session(tmp_path)
    message = snapshot(
        client,
        selection={
            "loaded": True,
            "tab": "상승",
            "regular": True,
            "regular_disabled": True,
            "regular_equivalent": True,
            "regular_equivalence_label": "지금은 실시간과 정규장 기준이 같아요",
        },
    )
    assert client.handle(message)["error"] == "selection_mismatch"


def test_unknown_stream_and_invalid_request_are_fixed_errors(tmp_path):
    client = session(tmp_path)
    assert client.handle({"id": 9, "type": "begin", "stream": "secret"}) == {
        "id": 9,
        "type": "begin",
        "ok": False,
        "error": "unknown_stream",
    }
    assert client.handle({"id": True, "type": "ping"})["ok"] is False
    assert client.handle({"id": 3, "type": "secret", "html": "PRIVATE"}) == {
        "id": 3,
        "type": "secret",
        "ok": False,
        "error": "invalid_message",
    }


def test_lane_binding_selection_and_single_consumption(tmp_path):
    client = session(tmp_path)
    message = snapshot(client)
    assert (
        client.handle({"id": 3, "type": "begin", "stream": "market_loss_live"})["error"]
        == "lane_busy"
    )
    message["selection"]["tab"] = "하락"
    assert client.handle(message)["error"] == "selection_mismatch"
    message["selection"]["tab"] = "상승"
    result = client.handle(message)
    assert result["ok"] is True and result["status"] == "partial"
    assert client.handle(message)["error"] == "invalid_job"


def test_json_generation_and_regular_never_use_live_quote(tmp_path):
    client = session(tmp_path)
    message = snapshot(
        client, "market_gain_regular", selection={"loaded": True, "tab": "상승", "regular": True}
    )
    message["json_responses"] = [
        {
            "url": "https://www.saveticker.com/api/stocks/api/v1/tickers/popular",
            "status": 200,
            "job_id": message["job_id"],
            "data": {"rankings": [{"symbol": "TEST", "price": 123, "changePercent": 5}]},
        }
    ]
    result = client.handle(message)
    assert result["count"] == 0 and result["status"] == "partial"


def test_json_persists_canonical_items_without_payload(tmp_path):
    client = session(tmp_path)
    message = snapshot(client)
    message["json_responses"] = [
        {
            "url": "https://www.saveticker.com/api/stocks/api/v1/tickers/popular",
            "status": 200,
            "job_id": message["job_id"],
            "data": {
                "rankings": [
                    {
                        "symbol": "TEST",
                        "price": 123,
                        "changePercent": 5,
                        "secret": "PRIVATE_SENTINEL",
                    }
                ]
            },
        }
    ]
    result = client.handle(message)
    assert result["count"] == 1
    saved = json.loads((tmp_path / "data/latest/market_gain_live.json").read_text())
    assert saved["items"][0]["price"] == "123"
    assert "PRIVATE_SENTINEL" not in (tmp_path / "data/feed.sqlite3").read_bytes().decode(
        errors="ignore"
    )
    assert datetime.fromisoformat(saved["observed_at"]) > datetime(2026, 1, 1, tzinfo=UTC)


def test_snapshot_size_and_wrong_url_are_rejected(tmp_path):
    client = session(tmp_path)
    message = snapshot(client)
    message["html"] = "x" * (4 * 1024 * 1024 + 1)
    assert client.handle(message)["error"] == "snapshot_too_large"
    message["html"] = ""
    message["page_url"] = "https://evil.example/watchlist"
    assert client.handle(message)["error"] == "invalid_url"


def test_job_expiry_and_pdf_without_current_report_are_rejected(tmp_path, monkeypatch):
    client = session(tmp_path)
    message = snapshot(client)
    monkeypatch.setattr(native, "_now", lambda: datetime(2099, 1, 1, tzinfo=UTC))
    assert client.handle(message)["error"] == "invalid_job"
    assert (
        client.handle({"id": 7, "type": "pdf_begin", "item_id": "missing"})["error"]
        == "unknown_report"
    )
    assert (
        client.handle(
            {
                "id": 8,
                "type": "pdf",
                "item_id": "missing",
                "ticket": "bad",
                "download_name": "../secret.pdf",
            }
        )["error"]
        == "invalid_pdf_ticket"
    )


def frame(message):
    data = json.dumps(message).encode()
    return struct.pack("=I", len(data)) + data


def test_serve_frames_errors_then_ping_and_cleans_up_on_eof(tmp_path):
    output = io.BytesIO()
    native.serve(
        tmp_path,
        EXTENSION,
        ORIGIN,
        stdin=io.BytesIO(frame({"id": 1, "type": "unknown"}) + frame({"id": 2, "type": "ping"})),
        stdout=output,
    )
    data = output.getvalue()
    size = struct.unpack("=I", data[:4])[0]
    assert json.loads(data[4 : 4 + size])["error"] == "invalid_message"
    second = data[4 + size :]
    assert json.loads(second[4:]) == {"id": 2, "type": "pong", "ok": True}
    assert (
        json.loads((tmp_path / "runtime/service.json").read_text())["reason"]
        == "extension_disconnected"
    )
    from trader.daemon import Lock

    assert not Lock(tmp_path / "runtime/service.lock").held()


def test_invalid_origin_and_oversized_frame_do_not_create_store(tmp_path):
    for origin, data in [(ORIGIN + "evil", b""), (ORIGIN, struct.pack("=I", 64 * 1024 * 1024 + 1))]:
        output = io.BytesIO()
        native.serve(tmp_path, EXTENSION, origin, stdin=io.BytesIO(data), stdout=output)
        assert b"PRIVATE" not in output.getvalue()
    assert not (tmp_path / "data/feed.sqlite3").exists()


def test_report_pdf_ticket_path_cache_integrity_and_single_use(tmp_path, monkeypatch):
    from pathlib import Path

    from pypdf import PdfWriter

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    client = session(tmp_path)
    job = client.handle({"id": 1, "type": "begin", "stream": "news_reports"})
    result = client.handle(
        {
            "id": 2,
            "type": "snapshot",
            "stream": "news_reports",
            "job_id": job["job_id"],
            "page_url": "https://www.saveticker.com/news",
            "selection": {"loaded": True, "source": "SAVE 리포트"},
            "html": (
                "<div><p>2026년 10월 20일 (화)</p>"
                '<button aria-label="PDF 다운로드">PDF</button></div>'
            ),
        }
    )
    item = result["report_items"][0]["id"]
    pdfjob = client.handle({"id": 3, "type": "pdf_begin", "item_id": item})
    folder = tmp_path / "home/Downloads/SaveTicker"
    folder.mkdir(parents=True)
    source = folder / pdfjob["download_name"]
    outside = tmp_path / "outside.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(outside)
    source.symlink_to(outside)
    message = {
        "id": 4,
        "type": "pdf",
        "item_id": item,
        "ticket": pdfjob["ticket"],
        "download_name": pdfjob["download_name"],
    }
    assert client.handle(message)["error"] == "invalid_pdf_path"
    source.unlink()
    writer.write(source)
    saved = client.handle(message)
    assert saved["ok"] and saved["attachment"]["pages"] == 1
    assert client.handle(message)["error"] == "invalid_pdf_ticket"
    assert client.handle({"id": 5, "type": "pdf_begin", "item_id": item})["error"] == "pdf_cached"
    (tmp_path / "data" / saved["attachment"]["pdf_path"]).write_bytes(b"corrupt")
    assert client.handle({"id": 6, "type": "pdf_begin", "item_id": item})["ok"]


def test_service_lock_contention_never_saves(tmp_path):
    from trader.daemon import Lock

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    lock = Lock(runtime / "service.lock")
    assert lock.acquire()
    try:
        native.serve(
            tmp_path,
            EXTENSION,
            ORIGIN,
            stdin=io.BytesIO(frame({"id": 1, "type": "ping"})),
            stdout=io.BytesIO(),
        )
        assert not (tmp_path / "data/feed.sqlite3").exists()
    finally:
        lock.release()


def test_repeated_news_list_keeps_prior_successful_detail(tmp_path):
    from trader.saveticker.dom import identity
    from trader.saveticker.models import Capture, Item
    from trader.saveticker.store import Store

    title = "Synthetic persistent article"
    original = Item(
        id=identity("news", "Publisher", title),
        title=title,
        publisher="Publisher",
        body="Previously collected full body",
        kind="news",
        detail_status="ok",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    store = Store(tmp_path / "data")
    store.save(
        Capture(
            stream="news_all",
            page_url="https://www.saveticker.com/news",
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
            items=[original],
        )
    )
    client = session(tmp_path)
    job = client.handle({"id": 1, "type": "begin", "stream": "news_all"})
    html = (
        '<div id="news-tabpanel"><div data-read="true" role="button" '
        'aria-label="Synthetic persistent article"><div><div><div>Publisher</div></div>'
        "<span>1분 전</span></div><div><div>3</div></div></div></div>"
    )
    result = client.handle(
        {
            "id": 2,
            "type": "snapshot",
            "stream": "news_all",
            "job_id": job["job_id"],
            "page_url": "https://www.saveticker.com/news",
            "html": html,
            "selection": {"loaded": True, "source": "전체", "category": "전체"},
        }
    )
    assert result["ok"] is True
    latest = json.loads((tmp_path / "data/latest/news_all.json").read_text())
    assert latest["items"][0]["body"] == "Previously collected full body"
    assert latest["items"][0]["detail_status"] == "ok"
    assert datetime.fromisoformat(latest["items"][0]["published_at"]) == datetime(
        2026, 1, 1, tzinfo=UTC
    )


def test_auth_snapshot_keeps_last_good_and_never_records_arbitrary_reason(tmp_path):
    client = session(tmp_path)
    message = snapshot(client)
    message["json_responses"] = [
        {
            "url": "https://www.saveticker.com/api/stocks/api/v1/tickers/popular",
            "status": 200,
            "job_id": message["job_id"],
            "data": {"rankings": [{"symbol": "TEST", "price": 123}]},
        }
    ]
    assert client.handle(message)["count"] == 1
    latest_path = tmp_path / "data/latest/market_gain_live.json"
    last_good = latest_path.read_bytes()
    message = snapshot(client, status="auth_required", reason="SECRET_HTML")
    message.pop("selection")
    assert client.handle(message)["status"] == "auth_required"
    assert latest_path.read_bytes() == last_good
    assert "SECRET_HTML" not in (tmp_path / "data/status.json").read_text()


def test_stale_json_generation_uses_dom_instead_of_stale_quote(tmp_path):
    client = session(tmp_path)
    message = snapshot(client)
    message["html"] = (
        "<div data-stretched-link-card><h3>Local</h3><span>LOCAL</span>"
        "<span>$22</span><span>+1%</span></div>"
    )
    message["json_responses"] = [
        {
            "url": "https://www.saveticker.com/api/stocks/api/v1/tickers/popular",
            "status": 200,
            "job_id": "stale",
            "data": {"rankings": [{"symbol": "STALE", "price": 999}]},
        }
    ]
    assert client.handle(message)["count"] == 1
    data = json.loads((tmp_path / "data/latest/market_gain_live.json").read_text())
    assert data["items"][0]["tickers"] == ["LOCAL"]
    assert data["status"] == "partial"


def test_json_auth_response_persists_block_and_consumes_job(tmp_path):
    client = session(tmp_path)
    message = snapshot(client)
    message["json_responses"] = [
        {
            "url": "https://www.saveticker.com/api/stocks/api/v1/tickers/popular",
            "status": 403,
            "job_id": message["job_id"],
            "data": {},
        }
    ]
    result = client.handle(message)
    assert result["ok"] is True and result["status"] == "auth_required"
    state = json.loads((tmp_path / "runtime/runner-market.json").read_text())
    assert state["state"] == "auth_required"
    assert client.handle(message)["error"] == "invalid_job"


def test_initialization_failure_releases_both_locks_without_diagnostics(tmp_path, monkeypatch):
    from trader.daemon import Lock

    def fail(_root):
        raise RuntimeError("SECRET_DIAGNOSTIC")

    monkeypatch.setattr(native, "NativeSession", fail)
    output = io.BytesIO()
    native.serve(tmp_path, EXTENSION, ORIGIN, stdin=io.BytesIO(), stdout=output)
    assert json.loads(output.getvalue()[4:])["error"] == "host_error"
    assert b"SECRET_DIAGNOSTIC" not in output.getvalue()
    assert not Lock(tmp_path / "runtime/service.lock").held()
    assert not Lock(tmp_path / "runtime/bridge-host.lock").held()


def test_malformed_frames_terminate_without_processing_following_ping(tmp_path):
    for index, invalid in enumerate(
        (
            b"\x01",
            struct.pack("=I", 10) + b"{}",
            struct.pack("=I", 1) + b"\xff",
            struct.pack("=I", 1) + b"{",
        )
    ):
        output = io.BytesIO()
        native.serve(
            tmp_path / str(index), EXTENSION, ORIGIN, stdin=io.BytesIO(invalid), stdout=output
        )
        assert json.loads(output.getvalue()[4:])["error"] == "invalid_frame"
        assert not (tmp_path / str(index) / "data/feed.sqlite3").exists()


def test_expired_pdf_and_reconnected_session_reject_old_ticket(tmp_path, monkeypatch):
    from trader.saveticker.models import Capture, Item
    from trader.saveticker.store import Store

    Store(tmp_path / "data").save(
        Capture(
            stream="news_reports",
            page_url="https://www.saveticker.com/news",
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
            items=[Item(id="report", title="Report", kind="report")],
        )
    )
    client = session(tmp_path)
    job = client.handle({"id": 1, "type": "pdf_begin", "item_id": "report"})
    message = {
        "id": 2,
        "type": "pdf",
        "item_id": "report",
        "ticket": job["ticket"],
        "download_name": job["download_name"],
    }
    assert session(tmp_path).handle(message)["error"] == "invalid_pdf_ticket"
    monkeypatch.setattr(native, "_now", lambda: datetime(2099, 1, 1, tzinfo=UTC))
    assert client.handle(message)["error"] == "invalid_pdf_ticket"


def test_ping_replaces_old_supervisor_heartbeat_with_native_backend(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "runner.json").write_text('{"pid": -1, "state": "stopped"}')
    result = session(tmp_path).handle({"id": 1, "type": "ping"})
    assert result["type"] == "pong"
    row = json.loads((runtime / "runner.json").read_text())
    assert row["pid"] > 0 and row["backend"] == "extension" and row["state"] == "collecting"


def test_disconnect_state_write_failure_still_releases_locks(tmp_path, monkeypatch):
    from trader.daemon import Lock

    def fail_disconnect(self):
        raise OSError("SECRET_DIAGNOSTIC")

    monkeypatch.setattr(native.NativeSession, "disconnect", fail_disconnect)
    native.serve(tmp_path, EXTENSION, ORIGIN, stdin=io.BytesIO(), stdout=io.BytesIO())
    assert not Lock(tmp_path / "runtime/service.lock").held()
    assert not Lock(tmp_path / "runtime/bridge-host.lock").held()


def test_old_and_unrelated_auth_responses_cannot_block_current_job(tmp_path):
    client = session(tmp_path)
    message = snapshot(client)
    message["json_responses"] = [
        {
            "url": "https://www.saveticker.com/api/stocks/api/v1/tickers/popular",
            "status": 403,
            "job_id": "old-job",
            "data": {},
        },
        {
            "url": "https://evil.example/api/news/list",
            "status": 403,
            "job_id": message["job_id"],
            "data": {},
        },
    ]
    result = client.handle(message)
    assert result["ok"] is True and result["status"] == "partial"


def test_json_with_userinfo_or_nonstandard_port_is_not_a_site_response(tmp_path):
    client = session(tmp_path)
    message = snapshot(client)
    message["json_responses"] = [
        {
            "url": url,
            "status": 200,
            "job_id": message["job_id"],
            "data": {"rankings": [{"symbol": "WRONG", "price": 1}]},
        }
        for url in (
            "https://credentials@www.saveticker.com/api/stocks/api/v1/tickers/popular",
            "https://www.saveticker.com:8443/api/stocks/api/v1/tickers/popular",
        )
    ]
    result = client.handle(message)
    assert result["ok"] is True and result["count"] == 0


def test_failed_snapshot_is_not_a_collecting_lane(tmp_path):
    client = session(tmp_path)
    message = snapshot(client, status="error", reason="selection_mismatch")
    result = client.handle(message)
    assert result["ok"] is True and result["status"] == "error"
    row = json.loads((tmp_path / "runtime/runner-market.json").read_text())
    assert row["state"] == "error"
