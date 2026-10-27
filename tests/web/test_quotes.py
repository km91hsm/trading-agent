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


def test_snapshot_prices_batches_latest_per_ticker(conn):
    """전체 검토 3차(성능): 종목별 쿼리 대신 한 번에, 종목마다 가장 최근 날짜(같은 날이면 큰 id)."""
    rows = [
        ("a", "2026-10-07", "005930", "69000"),
        ("b", "2026-10-08", "005930", "70500"),
        ("a", "2026-10-08", "000660", "180000"),
        ("a", "2026-10-09", "000660", None),  # 현재가 없는 행은 건너뛴다
    ]
    for acct, d, t, px in rows:
        conn.execute(
            "INSERT INTO positions_snapshot(account_id, market, date, ticker, qty, current_price, "
            "created_at) VALUES (?, 'KR', ?, ?, 1, ?, 'x')",
            (acct, d, t, px),
        )
    conn.commit()
    got = quotes_mod._snapshot_prices(conn, "KR", ["005930", "000660", "035420"])
    assert {t: (v["price"], v["asof"]) for t, v in got.items()} == {
        "005930": ("70500", "2026-10-08"),
        "000660": ("180000", "2026-10-08"),
    }
    assert quotes_mod._snapshot_prices(conn, "KR", []) == {}


def test_quote_cache_drops_old_entries_and_idle_locks():
    quotes_mod._key_lock(("KR", "OLD"))
    quotes_mod._remember(("KR", "OLD"), {"price": "1"}, now=0.0)
    quotes_mod._remember(("KR", "NEW"), {"price": "2"}, now=quotes_mod.KEEP_S + 1)
    assert set(quotes_mod._cache) == {("KR", "NEW")}
    assert ("KR", "OLD") not in quotes_mod._key_locks


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


def test_quotes_concurrent_misses_call_kis_once(project, conn):
    """두 요청이 동시에 캐시 미스여도 KIS 호출은 한 번(종목별 락 + 다시 보기)."""
    import threading
    import time as _t

    c, deps = _client(project, OPEN)
    orig = deps.market.get_price_kr

    def slow(t):
        _t.sleep(0.2)
        return orig(t)

    deps.market.get_price_kr = slow
    ths = [
        threading.Thread(
            target=lambda: c.get("/api/quotes", params={"market": "KR", "tickers": "005930"})
        )
        for _ in range(3)
    ]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    assert deps.market.calls.count("get_price_kr") == 1


class FakeHub:
    def __init__(self, ticks, connected=True):
        self.ticks, self.connected, self.requested = ticks, connected, []

    def request(self, tickers):
        self.requested.append(list(tickers))
        return {t: v for t, v in self.ticks.items() if t in tickers}

    def status(self):
        return {"connected": self.connected, "subscribed": len(self.ticks)}


def test_quotes_kr_uses_ws_hub_first(project, conn):
    """웹소켓 틱이 있는 종목은 허브 값, 없는 종목만 REST. stream 이면 화면이 1초 주기."""
    c, deps = _client(project, OPEN)
    tick = {"price": "71600", "change_pct": 1.2, "asof": "x", "stale": False, "source": "ws"}
    deps.quote_hub = FakeHub({"000660": tick})
    r = c.get("/api/quotes", params={"market": "KR", "tickers": "005930,000660"}).json()
    assert r["quotes"]["000660"] == tick and r["stream"] is True
    assert r["quotes"]["005930"]["price"] == "71500"  # REST
    assert deps.market.calls.count("get_price_kr") == 1
    assert deps.quote_hub.requested == [["005930", "000660"]]


def test_quotes_us_and_closed_never_touch_hub(project, conn):
    c, deps = _client(project, OPEN)
    deps.quote_hub = FakeHub({})
    r = c.get("/api/quotes", params={"market": "US", "tickers": "AAPL"}).json()
    assert r["stream"] is False and deps.quote_hub.requested == []
    c2, deps2 = _client(project, CLOSED)
    deps2.quote_hub = FakeHub({})
    assert (
        c2.get("/api/quotes", params={"market": "KR", "tickers": "005930"}).json()["stream"]
        is False
    )
    assert deps2.quote_hub.requested == []


class BookHub(FakeHub):
    def request_indices(self):
        return {"KOSPI": {"price": "2650.12", "change_pct": -0.0085, "asof": "x"}}

    def request_book(self, ticker):
        return {
            "asks": [["71100", "10"]],
            "bids": [["71000", "5"]],
            "total_ask": "10",
            "total_bid": "5",
            "time": "093015",
            "asof": "x",
        }


def test_indices_and_book_routes(project, conn):
    c, deps = _client(project, OPEN)
    deps.quote_hub = BookHub({})
    r = c.get("/api/quotes/indices").json()
    assert r["session"] == "open" and r["indices"]["KOSPI"]["price"] == "2650.12"
    b = c.get("/api/quotes/book", params={"ticker": "005930"}).json()
    assert b["book"]["asks"][0] == ["71100", "10"]
    assert c.get("/api/quotes/book", params={"ticker": "../x"}).status_code == 400
    c2, deps2 = _client(project, CLOSED)
    deps2.quote_hub = BookHub({})
    assert c2.get("/api/quotes/indices").json() == {
        "session": "closed",
        "asof": CLOSED.isoformat(),
        "indices": {},
    }
