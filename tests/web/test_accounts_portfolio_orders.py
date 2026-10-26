from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from helpers_agents import ok_decision
from helpers_web import AUTH, make_deps

from trader.ledger import repo
from trader.web.app import create_app

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


def test_refresh_snapshots_under_market_run_date(project, conn):
    """잔고 새로고침 스냅샷 날짜도 executor·monitor 와 같은 운영일(US 는 뉴욕)이어야 한다."""
    from datetime import UTC, datetime

    deps = make_deps(project)
    deps.now = lambda: datetime(2026, 10, 9, 23, 0, tzinfo=UTC)  # 08:00 KST 10-10 = 19:00 ET 10-09
    c = TestClient(create_app(deps))
    r = c.post("/api/portfolio/refresh?account_id=paper-main&market=US", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["date"] == "2026-10-09"


def test_ensure_fresh_refreshes_stale_snapshot_once_and_throttles(client, deps, conn):
    """페이지 접속 갱신(토큰 없음): 정규장일 때만, 오래된 스냅샷만, 시장별 30초에 한 번(조회만)."""
    from datetime import UTC, datetime

    from trader.web.routers import portfolio as pf

    pf._ENSURE_LAST.clear()
    deps.now = lambda: datetime(2026, 10, 24, 3, 0, tzinfo=UTC)  # 토요일
    assert client.post("/api/portfolio/ensure-fresh?market=KR").json()["skipped"] == "정규장 아님"
    deps.now = lambda: datetime(2026, 10, 26, 1, 0, tzinfo=UTC)  # 월 10:00 KST
    r = client.post("/api/portfolio/ensure-fresh?market=KR").json()
    assert "paper-main" in r["refreshed"]
    assert all(not b.orders for b in deps.brokers.values())  # 주문 없음
    assert client.post("/api/portfolio/ensure-fresh?market=KR").json()["throttled"] is True
    pf._ENSURE_LAST.clear()
    r = client.post("/api/portfolio/ensure-fresh?market=KR").json()
    assert "paper-main" in r["skipped"]  # 방금 갱신해 신선함
    assert client.post("/api/portfolio/ensure-fresh?market=XX").status_code == 400


def test_balance_refresh_skips_when_same_market_already_running(deps, conn):
    """데몬 잡과 웹 접속 갱신이 같은 시장 잔고를 겹쳐 읽지 않는다(Codex 리뷰)."""
    from datetime import UTC, datetime

    from trader.orchestrator import balance

    lock = balance._RUNNING["KR"]
    lock.acquire()
    try:
        out = balance.refresh(
            conn, accounts=deps.accounts, brokers=deps.brokers, market="KR",
            settings=deps.settings, now=datetime(2026, 10, 26, 1, 0, tzinfo=UTC),
        )
    finally:
        lock.release()
    assert out["busy"] is True and out["refreshed"] == []
