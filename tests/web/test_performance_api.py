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
