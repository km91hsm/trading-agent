"""Phase 24 단계 2: /api/quotes — 장중만 KIS, TTL 캐시, 바쁘면 스냅샷 값(stale)."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from helpers_web import make_deps

from trader.web.app import create_app
from trader.web.routers import quotes as quotes_mod


@pytest.fixture(autouse=True)
def _clear_cache():
    quotes_mod._cache.clear()
    yield
    quotes_mod._cache.clear()


def _client(project, now):
    deps = make_deps(project)
    deps.now = lambda: now
    return TestClient(create_app(deps)), deps


def _snap(conn):
    conn.execute(
        "INSERT INTO positions_snapshot(account_id, market, date, ticker, qty, avg_price, "
        "current_price, eval_amount, nav, cash, created_at) VALUES "
        "('paper-main','KR','2026-10-08','005930',1,'70000','70500','70500','1000000','900000',"
        "'2026-10-08T07:00:00+00:00')"
    )
    conn.commit()


OPEN = datetime(2026, 10, 9, 2, 0, tzinfo=UTC)  # 금 11:00 KST
CLOSED = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)  # 17:00 KST


def test_quotes_open_session_calls_kis(project, conn):
    c, deps = _client(project, OPEN)
    r = c.get("/api/quotes", params={"market": "KR", "tickers": "005930"}).json()
    assert r["session"] == "open"
    q = r["quotes"]["005930"]
    assert q["price"] == "71500" and q["stale"] is False
    assert deps.market.calls.count("get_price_kr") == 1


def test_quotes_closed_session_no_kis_call(project, conn):
    _snap(conn)
    c, deps = _client(project, CLOSED)
    r = c.get("/api/quotes", params={"market": "KR", "tickers": "005930,000660"}).json()
    assert r["session"] == "closed"
    assert r["quotes"]["005930"] == {
        "price": "70500",
        "change_pct": None,
        "asof": "2026-10-08",
        "stale": True,
    }
    assert "000660" not in r["quotes"]
    assert "get_price_kr" not in deps.market.calls


def test_quotes_ttl_cache_dedupes(project, conn):
    c, deps = _client(project, OPEN)
    for _ in range(3):
        c.get("/api/quotes", params={"market": "KR", "tickers": "005930"})
    assert deps.market.calls.count("get_price_kr") == 1


def test_quotes_rate_busy_returns_stale(project, conn, monkeypatch):
    from fastapi import HTTPException

    _snap(conn)
    c, deps = _client(project, OPEN)

    def busy(*a, **k):
        raise HTTPException(status_code=503, detail="busy")

    monkeypatch.setattr(deps.market, "get_price_kr", busy)
    r = c.get("/api/quotes", params={"market": "KR", "tickers": "005930"})
    assert r.status_code == 200
    assert r.json()["quotes"]["005930"]["stale"] is True


def test_quotes_limit_20_and_validation(project, conn):
    c, _ = _client(project, OPEN)
    many = ",".join(f"{i:06d}" for i in range(21))
    assert c.get("/api/quotes", params={"market": "KR", "tickers": many}).status_code == 400
    assert c.get("/api/quotes", params={"market": "XX", "tickers": "005930"}).status_code == 400
    assert c.get("/api/quotes", params={"market": "KR", "tickers": "../x"}).status_code == 400


def test_quotes_us_exchange_suffix(project, conn):
    c, deps = _client(project, datetime(2026, 10, 9, 14, 0, tzinfo=UTC))
    r = c.get("/api/quotes", params={"market": "US", "tickers": "AAPL:NAS"}).json()
    assert r["quotes"]["AAPL"]["price"] == "227.55"
