from datetime import date
from decimal import Decimal

from helpers_agents import ok_decision
from helpers_web import AUTH

from trader.ledger import repo

D = date(2026, 10, 9)


def _seed_orders(conn):
    dec_id = repo.ingest_checkpoint(conn, D, "KR", "005930", "30_decision", {"data": ok_decision()})
    for n in ("1", "2"):
        repo.insert_order(
            conn,
            decision_id=dec_id,
            account_id="paper-main",
            ticker="005930",
            market="KR",
            side="BUY",
            qty=1,
            order_type="LIMIT",
            price=Decimal("71000"),
            status="accepted",
            order_no=n,
            message="",
            mode="auto",
        )
    return dec_id


def test_accounts_lists_modes(client):
    rows = client.get("/api/accounts").json()
    assert [a["id"] for a in rows] == ["paper-main", "real-main"]
    assert rows[0]["mode"] == "auto" and rows[1]["mode"] == "off"
    assert rows[0]["stance"] == "normal" and rows[0]["pending_approvals"] == 0
    assert "cano" not in rows[0]


def test_account_id_required(client):
    for path in ("/api/orders", "/api/approvals", "/api/portfolio?market=KR", "/api/risk-checks"):
        assert client.get(path).status_code == 400, path
    assert client.get("/api/orders?account_id=ghost").status_code == 400
    assert client.get("/api/portfolio?account_id=paper-main").status_code == 400  # market 필수


def test_orders_isolated_per_account(client, conn):
    _seed_orders(conn)
    assert len(client.get("/api/orders?account_id=paper-main").json()) == 2
    assert client.get("/api/orders?account_id=real-main").json() == []
    assert client.get("/api/approvals?account_id=real-main").json() == []
    assert client.get("/api/risk-checks?account_id=real-main").json() == []


def test_portfolio_refresh_requires_token_and_snapshots(client, conn):
    url = "/api/portfolio/refresh?account_id=paper-main&market=KR"
    assert client.post(url).status_code == 401
    r = client.post(url, headers=AUTH)
    assert r.status_code == 200 and r.json()["nav"] == "10000000"
    p = client.get("/api/portfolio?account_id=paper-main&market=KR").json()
    assert p["snapshot"]["nav"] == "10000000" and p["orders"] == [] and p["approvals"] == []
    assert client.get("/api/portfolio?account_id=real-main&market=KR").json()["snapshot"] is None
    assert (
        client.post(
            "/api/portfolio/refresh?account_id=real-main&market=US", headers=AUTH
        ).status_code
        == 400
    )
