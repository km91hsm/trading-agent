from datetime import date
from decimal import Decimal

from helpers_agents import ok_decision
from helpers_web import AUTH, NOW

from trader.agents.schemas import Decision
from trader.control import control
from trader.ledger import repo
from trader.orchestrator import executor

D = date(2026, 10, 9)


def _pending_approval(deps) -> int:
    """paper-main 을 approve 모드로 바꿔 BUY 결정을 실행 → 승인 요청 id."""
    conn = deps.conn_factory()
    try:
        control.set_account_mode(conn, deps.account("paper-main"), "approve")
        dec = Decision(**ok_decision())
        repo.ingest_checkpoint(conn, D, "KR", "005930", "30_decision", {"data": dec.model_dump()})
        ectx = executor.ExecContext(
            settings=deps.settings,
            accounts=deps.accounts,
            conn=conn,
            brokers=deps.brokers,
            notifier=deps.notifier,
            now=deps.now,
        )
        res = executor.execute(
            ectx, dec, date=D, market="KR", price=Decimal("71500"), dry_run=False
        )
        return next(r for r in res if r["account_id"] == "paper-main")["approval_id"]
    finally:
        conn.close()


def test_all_control_endpoints_401_without_token(client):
    for path, body in (
        ("/api/control/approve", {"approval_id": 1}),
        ("/api/control/reject", {"approval_id": 1}),
        ("/api/control/mode", {"account_id": "paper-main", "mode": "off"}),
        ("/api/control/pause", None),
        ("/api/control/resume", None),
    ):
        assert client.post(path, json=body).status_code == 401, path


def test_web_approve_twice_conflict(client, deps):
    aid = _pending_approval(deps)
    r = client.post("/api/control/approve", json={"approval_id": aid}, headers=AUTH)
    assert r.status_code == 200 and r.json()["order"]["order_no"] == "FAKE-1"
    r2 = client.post("/api/control/approve", json={"approval_id": aid}, headers=AUTH)
    assert r2.status_code == 409 and "이미" in r2.json()["detail"]
    assert len(deps.brokers["paper-main"].orders) == 1
    conn = deps.conn_factory()
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
    conn.close()
    assert (
        client.post("/api/control/approve", json={"approval_id": 999}, headers=AUTH).status_code
        == 409
    )


def test_web_approve_expired_conflict(client, deps):
    from datetime import timedelta

    aid = _pending_approval(deps)
    deps.now = lambda: NOW + timedelta(minutes=31)
    r = client.post("/api/control/approve", json={"approval_id": aid}, headers=AUTH)
    assert r.status_code == 409 and "만료" in r.json()["detail"]
    assert deps.brokers["paper-main"].orders == []


def test_web_reject(client, deps):
    aid = _pending_approval(deps)
    r = client.post("/api/control/reject", json={"approval_id": aid}, headers=AUTH)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert client.get("/api/approvals?account_id=paper-main&status=pending").json() == []
    assert (
        client.post("/api/control/reject", json={"approval_id": aid}, headers=AUTH).status_code
        == 409
    )


def test_web_mode_real_auto_requires_confirm(client, deps):
    r = client.post(
        "/api/control/mode", json={"account_id": "real-main", "mode": "auto"}, headers=AUTH
    )
    assert r.status_code == 400 and "LIVE" in r.json()["detail"]
    assert (
        next(a for a in client.get("/api/accounts").json() if a["id"] == "real-main")["mode"]
        == "off"
    )
    r = client.post(
        "/api/control/mode",
        json={"account_id": "real-main", "mode": "auto", "confirm": "LIVE"},
        headers=AUTH,
    )
    assert r.status_code == 200 and r.json()["mode"] == "auto"
    conn = deps.conn_factory()
    assert control.get_flag(conn, "live_confirmed:real-main")
    conn.close()
    assert (
        client.post(
            "/api/control/mode", json={"account_id": "ghost", "mode": "off"}, headers=AUTH
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/control/mode", json={"account_id": "paper-main", "mode": "fast"}, headers=AUTH
        ).status_code
        == 400
    )


def test_web_mode_paper_approve(client):
    r = client.post(
        "/api/control/mode", json={"account_id": "paper-main", "mode": "approve"}, headers=AUTH
    )
    assert r.status_code == 200
    assert client.get("/api/accounts").json()[0]["mode"] == "approve"


def test_pause_resume_flags(client):
    assert client.post("/api/control/pause", headers=AUTH).json()["paused"] is True
    assert client.post("/api/control/resume", headers=AUTH).json()["paused"] is False


def test_events_once_returns_snapshot(client):
    r = client.get("/api/events?once=1")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert "event: change" in r.text and '"paused"' in r.text and '"approvals_pending"' in r.text
