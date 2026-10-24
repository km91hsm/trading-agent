"""작업 대기열 소비(daemon._queue_tick)·heavy 락·부트스트랩·단일 종목 분석 요청."""

import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_daemon import SCHED, FakeScheduler, _deps

from trader import daemon, heartbeat
from trader import jobs as jobq
from trader.broker.fake import FakeMarket
from trader.config import Settings

NOW = datetime(2026, 10, 24, 1, 0, tzinfo=UTC)


def _hb(project):
    hb = heartbeat.Heartbeat(project / "state" / heartbeat.FILE_NAME)
    hb.start(pid=1, started_at=NOW, jobs=daemon.build_jobs(SCHED, Settings(), markets=["KR"]))
    return hb


def test_build_jobs_includes_job_queue_interval():
    jobs = {j.id: j for j in daemon.build_jobs(SCHED, Settings(), markets=["KR"])}
    assert jobs["job_queue"].kind == "job_queue" and jobs["job_queue"].interval_s == 10


def test_queue_tick_runs_request_and_records_runs_and_heartbeat(project, conn, monkeypatch):
    deps = _deps(conn, FakeMarket(), now=lambda: NOW)
    monkeypatch.setattr(daemon, "_scan_analyze_body", lambda d, m: {"targets": 0, "m": m})
    rid = jobq.enqueue(conn, "scan_analyze", {"market": "KR"}, source="web", now=NOW)
    hb = _hb(project)
    assert daemon._queue_tick(deps, hb) == {"request": rid, "kind": "scan_analyze"}
    req = jobq.list_recent(conn)[0]
    assert req["status"] == "ok" and req["result"] == {"targets": 0, "m": "KR"}
    runs = conn.execute("SELECT kind, status FROM runs").fetchall()
    assert [(r["kind"], r["status"]) for r in runs] == [("kr_scan_analyze", "ok")]
    assert hb._data["jobs"]["kr_scan_analyze"]["last_status"] == "ok"
    assert daemon._queue_tick(deps, hb) == {"skipped": "대기 없음"}


def test_queue_tick_marks_error_and_runs_row_on_exception(project, conn, monkeypatch):
    deps = _deps(conn, FakeMarket(), now=lambda: NOW)

    def boom(d, **kw):
        raise RuntimeError("폭발")

    monkeypatch.setattr(daemon, "_ml_train_body", boom)
    rid = jobq.enqueue(conn, "ml_train", {}, source="web", now=NOW)
    r = daemon._queue_tick(deps, _hb(project))  # 예외는 삼켜진다
    assert r == {"request": rid, "kind": "ml_train"}
    req = jobq.list_recent(conn)[0]
    assert req["status"] == "error" and "폭발" in req["error"]
    run = conn.execute("SELECT kind, status FROM runs").fetchone()
    assert run["kind"] == "ml_train" and run["status"] == "error"


def test_queue_tick_skips_while_heavy_lock_held(project, conn):
    deps = _deps(conn, FakeMarket(), now=lambda: NOW)
    jobq.enqueue(conn, "ml_train", {}, source="web", now=NOW)
    with deps.heavy_lock:
        assert daemon._queue_tick(deps, None) == {"skipped": "다른 무거운 잡 실행 중"}
    assert jobq.list_recent(conn)[0]["status"] == "queued"
    deps.stop_event.set()
    assert daemon._queue_tick(deps, None) == {"skipped": "종료 중"}


def test_cron_scan_analyze_waits_for_heavy_lock(conn, monkeypatch):
    deps = _deps(conn, FakeMarket(), now=lambda: datetime(2026, 10, 8, 3, 0, tzinfo=UTC))
    order = []
    monkeypatch.setattr(daemon, "_scan_analyze_body", lambda d, m: order.append("cron") or {})
    job = next(j for j in daemon.build_jobs(SCHED, Settings(), markets=["KR"]) if j.market)
    deps.heavy_lock.acquire()
    t = threading.Thread(target=daemon.job_body(deps, job))
    t.start()
    t.join(0.2)
    assert t.is_alive() and order == []  # 락을 기다린다
    order.append("queue-done")
    deps.heavy_lock.release()
    t.join(2)
    assert order == ["queue-done", "cron"]


def test_features_backfill_request_uses_features_fn_and_progress(project, conn):
    deps = _deps(conn, FakeMarket(), now=lambda: NOW)
    seen = {}

    def features_fn(action, markets, *, progress, **kw):
        seen.update(action=action, markets=markets, kw=kw)
        for i in range(3):
            progress(f"KR 00593{i}: 10행")
        return 30

    deps.features_fn = features_fn
    rid = jobq.enqueue(
        conn, "features_backfill", {"years": 2, "market": "KR"}, source="web", now=NOW
    )
    daemon._queue_tick(deps, None)
    req = jobq.list_recent(conn)[0]
    assert req["status"] == "ok" and req["result"] == {"rows": 30}
    assert seen == {"action": "backfill", "markets": ["KR"], "kw": {"years": 2}}
    assert req["progress"].startswith("[1] KR 005930")  # 1초 스로틀: 첫 호출만 기록
    assert rid == req["id"]
    # features_fn 이 없으면 error
    deps.features_fn = None
    jobq.enqueue(conn, "features_derive", {}, source="web", now=NOW)
    daemon._queue_tick(deps, None)
    assert "시세 클라이언트 없음" in jobq.list_recent(conn)[0]["error"]


def test_progress_writer_cancels_on_stop_event(conn):
    deps = _deps(conn, FakeMarket(), now=lambda: NOW)

    def features_fn(action, markets, *, progress, **kw):
        progress("one")
        deps.stop_event.set()
        progress("two")
        return 1

    deps.features_fn = features_fn
    jobq.enqueue(conn, "features_snapshot", {}, source="web", now=NOW)
    daemon._queue_tick(deps, None)
    req = jobq.list_recent(conn)[0]
    assert req["status"] == "skipped" and req["error"] == "종료 중"


def test_analyze_ticker_request_runs_one_session_with_dry_run_precedence(conn):
    specs = []

    def runner(spec, *a, **k):
        specs.append(spec)
        return SimpleNamespace(ok=True, session_id="s", cost_usd=0.1)

    deps = _deps(conn, FakeMarket(), now=lambda: NOW)
    deps.session_runner = runner
    deps.dry_run = True
    deps.universe = SimpleNamespace(us=[SimpleNamespace(symbol="AAPL", exchange="NAS")], kr=[])
    jobq.enqueue(
        conn, "analyze_ticker", {"market": "KR", "ticker": "005930", "dry_run": False}, source="web"
    )
    daemon._queue_tick(deps, None)
    assert specs[0].tickers == [{"ticker": "005930", "exchange": None}]
    assert specs[0].dry_run is True  # 데몬 --dry-run 을 웹이 뒤집지 못한다
    req = jobq.list_recent(conn)[0]
    assert req["status"] == "ok" and req["result"]["ok"] == 1
    # US 거래소는 유니버스로 보강, 없으면 error
    jobq.enqueue(conn, "analyze_ticker", {"market": "US", "ticker": "AAPL"}, source="web")
    daemon._queue_tick(deps, None)
    assert specs[1].tickers == [{"ticker": "AAPL", "exchange": "NAS"}]
    jobq.enqueue(conn, "analyze_ticker", {"market": "US", "ticker": "ZZZZ"}, source="web")
    daemon._queue_tick(deps, None)
    assert "거래소 코드" in jobq.list_recent(conn)[0]["error"] and len(specs) == 2


def test_rl_train_request_passes_market_and_seeds(conn, monkeypatch):
    from trader.ml.rl import train as rl_train_mod

    seen = {}

    def fake(cfg, c, **kw):
        seen["seeds"] = cfg.models.rl_sizer.seeds
        seen["market"] = kw.get("market")
        return {"status": "trained", "version": "v", "summary": {}}

    monkeypatch.setattr(rl_train_mod, "train_sizer", fake)
    deps = _deps(conn, FakeMarket(), now=lambda: NOW)
    jobq.enqueue(conn, "rl_train", {"market": "US", "seeds": 2}, source="web")
    daemon._queue_tick(deps, None)
    assert seen == {"seeds": 2, "market": "US"}
    assert jobq.list_recent(conn)[0]["status"] == "ok"


@pytest.mark.parametrize(
    "has_files,market,recent_fail,expected",
    [
        (False, True, False, ["features_backfill", "ml_train"]),
        (True, True, False, []),
        (False, False, False, []),
        (False, True, True, []),
    ],
)
def test_bootstrap_queue(project, conn, has_files, market, recent_fail, expected):
    deps = _deps(conn, FakeMarket() if market else None, now=lambda: NOW)
    deps.features_fn = lambda *a, **k: 0
    if has_files:
        d = project / "state" / "features"
        d.mkdir(parents=True)
        (d / "2026-10-23.parquet").write_bytes(b"x")
    if recent_fail:
        rid = jobq.enqueue(conn, "features_backfill", {"years": 3}, source="daemon", now=NOW)
        jobq.claim_next(conn, now=NOW)
        jobq.finish(conn, rid, "error", error="x", now=NOW - timedelta(hours=1))
    r = daemon._bootstrap_queue(deps)
    assert r["queued"] == expected


def test_run_marks_stale_running_and_expires_old_queued(project, conn):
    old = NOW - timedelta(hours=7)
    a = jobq.enqueue(conn, "ml_train", {}, source="web", now=old)
    jobq.claim_next(conn, now=old)
    b = jobq.enqueue(conn, "rl_train", {}, source="web", now=old)
    deps = _deps(conn, FakeMarket(), now=lambda: NOW)
    (project / "state" / "features").mkdir(parents=True)
    (project / "state" / "features" / "x.parquet").write_bytes(b"x")
    daemon.run(deps, web=False, scheduler_factory=FakeScheduler)
    by = {r["id"]: r["status"] for r in jobq.list_recent(conn)}
    assert by[a] == "error" and by[b] == "skipped"


def test_listener_ignores_max_instances_for_job_queue(project):
    from apscheduler import events as ev

    sched = FakeScheduler()
    hb = _hb(project)
    daemon._install_heartbeat_listener(sched, hb)
    on_job = next(cb for cb, mask in sched.listeners if mask & ev.EVENT_JOB_MAX_INSTANCES)
    on_job(SimpleNamespace(code=ev.EVENT_JOB_MAX_INSTANCES, job_id="job_queue"))
    assert hb._data["jobs"]["job_queue"]["last_status"] is None
    on_job(SimpleNamespace(code=ev.EVENT_JOB_MAX_INSTANCES, job_id="kr_scan_analyze"))
    assert hb._data["jobs"]["kr_scan_analyze"]["last_status"] == "missed"
