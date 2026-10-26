from contextlib import closing
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
        ("/api/control/jobs", {"kind": "ml_train", "params": {}}),
        ("/api/control/jobs/cancel", {"id": 1}),
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


def test_web_approve_while_paused_conflict(client, deps):
    aid = _pending_approval(deps)
    assert client.post("/api/control/pause", headers=AUTH).status_code == 200
    r = client.post("/api/control/approve", json={"approval_id": aid}, headers=AUTH)
    assert r.status_code == 409 and "일시정지" in r.json()["detail"]
    assert deps.brokers["paper-main"].orders == []


def test_web_approve_broker_rejected_is_502(client, deps):
    from trader.broker.fake import FakeBroker

    aid = _pending_approval(deps)
    old = deps.brokers["paper-main"]
    deps.brokers["paper-main"] = FakeBroker(old.account, cash=old.cash, fail={"place_order"})
    r = client.post("/api/control/approve", json={"approval_id": aid}, headers=AUTH)
    assert r.status_code == 502 and "가짜 실패" in r.json()["detail"]


def test_events_endpoint_is_async_and_bounded(client):
    import inspect

    from trader.web import sse

    assert inspect.iscoroutinefunction(sse.events)
    r = client.get("/api/events?once=1")
    assert r.text.count("event: change") == 1


# --- 이연 회수: 관심종목 웹 제어·SSE 카운터 -------------------------------------------


def test_watchlist_api_token_and_validation(client, deps):
    assert client.get("/api/control/watchlist").json() == {"tickers": []}
    body = {"action": "add", "ticker": "005930"}
    assert client.post("/api/control/watchlist", json=body).status_code == 401
    h = {"Authorization": f"Bearer {deps.token}"}
    assert client.post("/api/control/watchlist", json=body, headers=h).json() == {
        "tickers": ["005930"]
    }
    bad = {"action": "add", "ticker": "bad ticker!"}
    assert client.post("/api/control/watchlist", json=bad, headers=h).status_code == 400
    rm = {"action": "remove", "ticker": "005930"}
    assert client.post("/api/control/watchlist", json=rm, headers=h).json() == {"tickers": []}
    assert client.get("/api/control/watchlist").json() == {"tickers": []}


def test_sse_snapshot_includes_triggers_and_watchlist(conn):
    from trader.control import control
    from trader.web import sse

    s0 = sse.snapshot(conn)
    assert "triggers" in s0 and "watchlist" in s0
    control.set_watchlist(conn, ["AAPL"])
    assert sse.snapshot(conn) != s0


def test_sse_snapshot_excludes_agent_calls_and_stream_reuses_connection(deps, monkeypatch):
    """세션 중 서브에이전트 호출마다 agent_calls 가 늘어 change 가 쏟아지고(프런트 전 페이지 재조회)
    틱마다 새 SQLite 연결을 열었다. 스냅샷에서 calls 를 빼고 스트림당 연결 1개를 쓴다."""
    import anyio

    from trader.web import sse

    with closing(deps.conn_factory()) as conn:
        assert "calls" not in sse.snapshot(conn)
    opened = {"n": 0}
    real_factory = deps.conn_factory

    def counting():
        opened["n"] += 1
        return real_factory()

    deps.conn_factory = counting
    tick = {"n": 0}

    def changing(conn):
        tick["n"] += 1
        return {"tick": tick["n"]}

    monkeypatch.setattr(sse, "snapshot", changing)

    async def run():
        return [e async for e in sse.stream(deps, interval_s=0, max_events=3)]

    events = anyio.run(run)
    assert len(events) == 3 and opened["n"] == 1


def test_web_approve_unknown_response_is_not_reported_as_rejection(client, deps):
    """전체 검토 W2: 응답 없는(unknown) 승인 주문은 502 '브로커 거부'가 아니라 200 + 경고."""
    from trader.broker.errors import UNKNOWN_RESPONSE_CODE, KisApiError
    from trader.broker.fake import FakeBroker

    class Timeout(FakeBroker):
        def place_order(self, req):
            raise KisApiError(UNKNOWN_RESPONSE_CODE, "ReadTimeout", "TTTC0012U")

    aid = _pending_approval(deps)
    old = deps.brokers["paper-main"]
    deps.brokers["paper-main"] = Timeout(old.account, cash=old.cash)
    r = client.post("/api/control/approve", json={"approval_id": aid}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["unknown"] is True and "다시 누르지" in body["message"]
    assert body["order"]["status"] == "unknown"


def test_approve_skips_when_decision_changed_after_request(client, deps):
    """전체 검토 O11: 요청 뒤 결정이 SELL·HOLD 로 바뀌면 옛 BUY 요청은 실행하지 않는다."""
    aid = _pending_approval(deps)
    conn = deps.conn_factory()
    try:
        conn.execute("UPDATE decisions SET action='HOLD'")
        conn.commit()
    finally:
        conn.close()
    r = client.post("/api/control/approve", json={"approval_id": aid}, headers=AUTH)
    assert r.status_code == 409 and "결정이 HOLD" in r.json()["detail"]
    assert deps.brokers["paper-main"].orders == []


def test_expired_pending_approval_no_longer_reserves_cash(deps):
    """전체 검토 O17: expire 잡이 안 돌아도 만료 시각이 지난 요청은 현금·중복 판정에서 빠진다."""
    from datetime import timedelta

    aid = _pending_approval(deps)
    conn = deps.conn_factory()
    try:
        a = repo.get_approval(conn, aid)
        later = (NOW + timedelta(hours=1)).isoformat()
        assert repo.today_order_amount(conn, "paper-main", D, market="KR") > 0
        assert repo.today_order_amount(conn, "paper-main", D, market="KR", now=later) == 0
        assert repo.has_open_order_or_approval(conn, a["decision_id"], "paper-main")
        assert not repo.has_open_order_or_approval(conn, a["decision_id"], "paper-main", now=later)
    finally:
        conn.close()


def test_sse_snapshot_detects_status_changes_without_new_rows(conn):
    """전체 검토 W5: MAX(id) 만 보면 체결·만료 같은 상태 변경(UPDATE)을 못 잡는다 →
    테이블별 지문(건수·최대 id·상태별 건수)으로 비교한다."""
    from trader.web import sse

    conn.execute(
        "INSERT INTO orders (account_id, ticker, market, side, qty, order_type, status, mode,"
        " created_at) VALUES ('paper-main','005930','KR','BUY',1,'LIMIT','accepted','auto','t')"
    )
    conn.execute(
        "INSERT INTO approvals (decision_id, account_id, order_json, status, requested_at,"
        " expires_at) VALUES (1,'paper-main','{}','pending','t','t')"
    )
    conn.commit()
    s0 = sse.snapshot(conn)
    conn.execute("UPDATE orders SET status='filled'")
    conn.commit()
    s1 = sse.snapshot(conn)
    assert s1["orders"] != s0["orders"]
    conn.execute("INSERT INTO fills (order_id, account_id, qty, price, filled_at)"
                 " VALUES (1,'paper-main',1,'100','t')")
    conn.commit()
    s2 = sse.snapshot(conn)
    assert s2["orders"] != s1["orders"]  # 체결 적재도 주문 표 재조회 대상
    conn.execute("UPDATE approvals SET status='expired'")
    conn.commit()
    s3 = sse.snapshot(conn)
    assert s3["approvals"] != s2["approvals"] and s3["approvals_pending"] == 0
    assert sse.snapshot(conn) == s3  # 바뀐 게 없으면 같은 지문
