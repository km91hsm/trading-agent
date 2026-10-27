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


def test_regime_endpoint_and_portfolio_sectors(client, conn):
    from datetime import UTC, datetime

    from trader.risk import regime

    r = client.get("/api/system/regime").json()
    assert r["enabled"] is True and r["markets"][0]["state"] is None
    assert r["instruments"]["KR"]["inverse"]["symbol"] == "114800"
    regime.update(
        conn,
        "KR",
        closes=[100.0] * 210 + [100.0 - i for i in range(1, 31)],
        cfg=client.app.state.deps.settings.hedge,
        today=datetime.now(UTC).date(),
    )
    r = client.get("/api/system/regime").json()
    kr = next(m for m in r["markets"] if m["market"] == "KR")
    assert kr["state"] == "bear" and kr["signals"]["ma_below"] is True
    # 포트폴리오 응답에 섹터 비중·상한
    client.post("/api/portfolio/refresh?account_id=paper-main&market=KR", headers=AUTH)
    p = client.get("/api/portfolio?account_id=paper-main&market=KR").json()
    assert p["max_sector_pct"] == 0.4 and isinstance(p["sectors"], list)
    assert (
        p["sectors"] and p["sectors"][-1]["sector"] in ("현금", "기타") or p["sectors"][0]["sector"]
    )


def test_usage_ensure_fresh_enqueues_probe_only_when_stale(client, deps, conn, monkeypatch):
    """페이지 접속·새로고침(토큰 없음): 마지막 사용량 관측이 5분보다 오래됐고 데몬이 살아 있으면
    대기열에 usage_probe 를 넣는다 — 60초에 한 번, 이미 대기 중이면 그대로, 다른 사이트는 403."""
    from datetime import timedelta

    from trader.web.routers import system as sysr

    deps.settings = deps.settings.model_copy(
        update={"llm": deps.settings.llm.model_copy(update={"usage_file": None})}
    )
    sysr._USAGE_ENSURE_LAST[0] = 0.0
    _alive(monkeypatch, False)
    assert client.post("/api/system/usage/ensure-fresh").json() == {"skipped": "데몬 꺼짐"}
    _alive(monkeypatch, True)
    r = client.post("/api/system/usage/ensure-fresh").json()
    assert r["queued"] is True and r["id"]
    kinds = [x[0] for x in conn.execute("SELECT kind FROM job_requests").fetchall()]
    assert kinds == ["usage_probe"]
    assert client.post("/api/system/usage/ensure-fresh").json() == {"throttled": True}
    sysr._USAGE_ENSURE_LAST[0] = 0.0
    assert client.post("/api/system/usage/ensure-fresh").json()["already"] is True
    now = deps.now()
    conn.execute(
        "INSERT INTO llm_rate_limits(observed_at, source, five_hour_util, raw_json) "
        "VALUES (?, 'probe', 0.2, '{}')",
        ((now - timedelta(minutes=1)).isoformat(),),
    )
    conn.commit()
    assert client.post("/api/system/usage/ensure-fresh").json()["fresh"] is True
    bad = client.post("/api/system/usage/ensure-fresh", headers={"Sec-Fetch-Site": "cross-site"})
    assert bad.status_code == 403
