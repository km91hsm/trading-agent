from datetime import date
from decimal import Decimal

from trader.ledger import repo


def test_api_performance_requires_account_and_market(client, conn):
    assert client.get("/api/performance").status_code == 400
    assert client.get("/api/performance?account_id=paper-main").status_code == 400
    assert client.get("/api/performance?account_id=ghost&market=KR").status_code == 400
    r = client.get("/api/performance?account_id=paper-main&market=KR")
    assert r.status_code == 200 and r.json()["metrics"]["days"] == 0
    repo.snapshot_positions(
        conn,
        account_id="paper-main",
        d=date(2026, 10, 8),
        market="KR",
        nav=Decimal(100),
        cash=Decimal(1),
        positions=[],
    )
    repo.snapshot_positions(
        conn,
        account_id="paper-main",
        d=date(2026, 10, 9),
        market="KR",
        nav=Decimal(103),
        cash=Decimal(1),
        positions=[],
    )
    r = client.get("/api/performance?account_id=paper-main&market=KR&days=30").json()
    assert abs(r["metrics"]["return"] - 0.03) < 1e-9 and r["nav"][-1]["value"] == "103"
    assert (
        client.get("/api/performance?account_id=real-main&market=KR").json()["metrics"]["days"] == 0
    )
    assert client.get("/api/performance?account_id=paper-main&market=KR&days=0").status_code == 422


def test_performance_report_is_cached_until_inputs_change(client, conn, monkeypatch):
    """SSE change 마다 두 번씩 불리는 성과 리포트는 입력(스냅샷·체결·주문·피처)이 같으면 재계산하지
    않는다 (리뷰 F22)."""
    from trader.ledger import performance
    from trader.web.routers import performance as perf_router

    perf_router._CACHE.clear()
    n = {"calls": 0}
    orig = performance.report

    def counting(*a, **kw):
        n["calls"] += 1
        return orig(*a, **kw)

    monkeypatch.setattr(performance, "report", counting)
    url = "/api/performance?account_id=paper-main&market=KR"
    seen = []
    orig_cached = perf_router.cached_report

    def spy(conn_, state, a, m, d):
        seen.append((perf_router._version(conn_, a, m, state), state))
        return orig_cached(conn_, state, a, m, d)

    monkeypatch.setattr(perf_router, "cached_report", spy)
    assert client.get(url).status_code == 200
    assert client.get(url).status_code == 200
    assert n["calls"] == 1, seen
    conn.execute(
        "INSERT INTO positions_snapshot(account_id, date, ticker, market, qty, created_at) "
        "VALUES ('paper-main', '2026-10-09', '_CASH', 'KR', 0, 'x')"
    )
    conn.commit()
    assert client.get(url).status_code == 200
    assert n["calls"] == 2


def test_api_performance_shadow_pairs(client, conn):
    r = client.get("/api/performance/shadow?market=KR").json()
    assert r["pairs"]["candidates_1n"]["n"] == 0 and r["recent"] == [] and r["min_pairs"] == 20
    for strat, ret in (("llm", 0.03), ("candidates_1n", 0.01), ("quant_topn", 0.02)):
        conn.execute(
            "INSERT INTO shadow_portfolios(market, date, strategy, weights_json, created_at, "
            "fwd_ret, matured_at) VALUES ('KR', '2026-10-01', ?, '{}', 'x', ?, 'x')",
            (strat, ret),
        )
    conn.commit()
    r = client.get("/api/performance/shadow?market=KR").json()
    assert r["pairs"]["candidates_1n"]["n"] == 1
    assert abs(r["pairs"]["candidates_1n"]["mean_diff"] - 0.02) < 1e-9
    assert abs(r["pairs"]["quant_topn"]["mean_diff"] - 0.01) < 1e-9
    assert r["recent"][0] == {
        "date": "2026-10-01",
        "llm": 0.03,
        "candidates_1n": 0.01,
        "quant_topn": 0.02,
    }
    assert client.get("/api/performance/shadow?market=US").json()["pairs"]["quant_topn"]["n"] == 0
