"""작업 대기열 웹 API: 토큰·데몬 생존·검증·중복·취소·SSE 스냅샷."""

from helpers_web import AUTH, NOW

from trader import jobs as jobq
from trader.web import sse
from trader.web.routers import jobs as jobs_router


def _alive(monkeypatch, value: bool):
    monkeypatch.setattr(jobs_router, "daemon_alive", lambda request: value)


def test_post_job_requires_token_and_running_daemon(client, monkeypatch):
    assert client.post("/api/control/jobs", json={"kind": "ml_train"}).status_code == 401
    _alive(monkeypatch, False)
    r = client.post("/api/control/jobs", json={"kind": "ml_train"}, headers=AUTH)
    assert r.status_code == 409 and "데몬이 꺼져" in r.json()["detail"]
    assert client.get("/api/jobs").json() == {"alive": False, "active": 0, "requests": []}


def test_post_job_validates_kind_and_params(client, monkeypatch):
    _alive(monkeypatch, True)
    r = client.post("/api/control/jobs", json={"kind": "nope"}, headers=AUTH)
    assert r.status_code == 400 and "알 수 없는 작업" in r.json()["detail"]
    r = client.post(
        "/api/control/jobs",
        json={"kind": "features_backfill", "params": {"years": 9}},
        headers=AUTH,
    )
    assert r.status_code == 400
    r = client.post("/api/control/jobs", json={"kind": "ml_train", "extra": 1}, headers=AUTH)
    assert r.status_code == 422


def test_post_job_enqueues_and_duplicates_409(client, deps, monkeypatch):
    _alive(monkeypatch, True)
    body = {"kind": "scan_analyze", "params": {"market": "KR"}}
    r = client.post("/api/control/jobs", json=body, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["status"] == "queued" and r.json()["label"] == "KR 스캔 → 분석"
    r2 = client.post("/api/control/jobs", json=body, headers=AUTH)
    assert r2.status_code == 409 and "이미" in r2.json()["detail"]
    g = client.get("/api/jobs").json()
    assert g["alive"] is True and g["active"] == 1
    assert g["requests"][0]["status"] == "queued" and g["requests"][0]["source"] == "web"
    assert g["requests"][0]["label"] == "KR 스캔 → 분석"


def test_cancel_only_queued(client, deps, monkeypatch):
    _alive(monkeypatch, True)
    rid = client.post("/api/control/jobs", json={"kind": "reflect"}, headers=AUTH).json()["id"]
    r = client.post("/api/control/jobs/cancel", json={"id": rid}, headers=AUTH)
    assert (
        r.status_code == 200
        and client.get("/api/jobs").json()["requests"][0]["status"] == "skipped"
    )
    rid2 = client.post("/api/control/jobs", json={"kind": "reflect"}, headers=AUTH).json()["id"]
    conn = deps.conn_factory()
    jobq.claim_next(conn, now=NOW)
    conn.close()
    r = client.post("/api/control/jobs/cancel", json={"id": rid2}, headers=AUTH)
    assert r.status_code == 409


def test_sse_snapshot_tracks_job_requests(conn):
    s0 = sse.snapshot(conn)
    assert s0["job_requests"] == 0 and s0["jobs_active"] == 0 and s0["jobs_done"] == 0
    rid = jobq.enqueue(conn, "ml_train", {}, source="web", now=NOW)
    s1 = sse.snapshot(conn)
    assert s1["job_requests"] == rid and s1["jobs_active"] == 1
    jobq.claim_next(conn, now=NOW)
    jobq.set_progress(conn, rid, "진행 중")
    assert sse.snapshot(conn) == s1  # progress 만으로는 change 가 나지 않는다
    jobq.finish(conn, rid, "ok", now=NOW)
    s2 = sse.snapshot(conn)
    assert s2["jobs_active"] == 0 and s2["jobs_done"] == rid
