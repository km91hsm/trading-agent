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
    assert rows[0]["stance_by_market"] == {"KR": "normal", "US": "normal"}
    assert rows[0]["pending_approvals"] == 0 and "stance" not in rows[0]
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


def test_accounts_show_effective_mode_not_raw_control(client, conn):
    """실전 auto 는 live_confirmed 없이는 executor 가 off 로 강등한다.

    화면도 실효 모드를 보여야 운영자가 '실전 자동 매매 중' 으로 오해하지 않는다."""
    from trader.control import control

    control.set_flag(conn, "mode:real-main", "auto")  # 확인 기록 없이 control 에 직접 쓴 상황
    rows = {a["id"]: a for a in client.get("/api/accounts").json()}
    assert rows["real-main"]["mode"] == "off" and "live_confirmed" in rows["real-main"]["mode_note"]
    assert rows["paper-main"]["mode"] == "auto" and rows["paper-main"]["mode_note"] is None


def test_portfolio_refresh_passes_memory_settings(client, conn, monkeypatch):
    """refresh 가 memory_settings 없이 build_portfolio 를 부르면 브레이커가 기본값으로 갱신된다."""
    from trader.orchestrator import executor

    seen = {}
    real = executor.build_portfolio

    def spy(conn_, broker, acct, market, today, memory_settings=None):
        seen["memory_settings"] = memory_settings
        return real(conn_, broker, acct, market, today, memory_settings=memory_settings)

    monkeypatch.setattr(executor, "build_portfolio", spy)
    r = client.post("/api/portfolio/refresh?account_id=paper-main&market=KR", headers=AUTH)
    assert r.status_code == 200 and seen["memory_settings"] is not None
