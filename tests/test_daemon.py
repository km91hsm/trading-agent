import json
import os
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from conftest import make_account

from trader import daemon
from trader.broker.fake import FakeBroker, FakeMarket
from trader.broker.models import Cash
from trader.config import ScheduleConfig, Settings
from trader.control import control
from trader.ledger import db
from trader.orchestrator import run_store

SCHED = ScheduleConfig.model_validate(
    {
        "kr": {
            "scan": "07:30",
            "analyze": "07:40",
            "monitor": {"start": "09:00", "end": "15:30", "interval_s": 60},
        },
        "us": {
            "scan": "21:30",
            "analyze": "21:40",
            "monitor": {"start": "23:30", "end": "06:00", "interval_s": 60},
        },
        "reflect": "16:30",
        "ml_train": {"weekday": "sat", "time": "10:00"},
    }
)


# --- lock -------------------------------------------------------------------------


def test_daemon_lock_rejects_second_instance(project):
    p = project / "state" / "daemon.lock"
    a = daemon.Lock(p)
    assert a.acquire() is True
    assert json.loads(p.read_text(encoding="utf-8"))["pid"] == os.getpid()
    b = daemon.Lock(p)
    assert b.acquire() is False
    a.release()
    assert not p.exists()


def test_stale_lock_is_taken_over(project):
    p = project / "state" / "daemon.lock"
    p.write_text(json.dumps({"pid": 2**22 + 12345, "started_at": "x"}), encoding="utf-8")
    assert daemon.Lock(p).acquire() is True
    assert json.loads(p.read_text(encoding="utf-8"))["pid"] == os.getpid()


def test_pid_alive_helper():
    from trader.platform.procs import pid_alive

    assert pid_alive(os.getpid()) is True
    assert pid_alive(2**22 + 12345) is False


# --- 창·건너뜀 -------------------------------------------------------------------------


def test_in_window_handles_midnight_crossing():
    assert daemon.in_window(time(10, 0), "09:00", "15:30")
    assert not daemon.in_window(time(15, 31), "09:00", "15:30")
    assert daemon.in_window(time(23, 45), "23:30", "06:00")
    assert daemon.in_window(time(2, 0), "23:30", "06:00")
    assert not daemon.in_window(time(12, 0), "23:30", "06:00")


def test_should_skip_weekend_holiday_paused(conn):
    market = FakeMarket(holidays={date(2026, 10, 9)})
    assert daemon.should_skip("KR", date(2026, 10, 10), market, conn) == "주말"
    assert daemon.should_skip("KR", date(2026, 10, 9), market, conn) == "휴장일"
    assert daemon.should_skip("KR", date(2026, 10, 8), market, conn) is None
    control.set_flag(conn, "paused", True)
    assert daemon.should_skip("KR", date(2026, 10, 8), market, conn) == "일시정지"
    assert daemon.should_skip("US", date(2026, 10, 8), None, conn) == "일시정지"


# --- 잡 ---------------------------------------------------------------------------


def test_build_jobs_registers_expected_ids_and_times():
    jobs = {j.id: j for j in daemon.build_jobs(SCHED, Settings(), markets=["KR", "US"])}
    assert {
        "kr_scan_analyze",
        "kr_monitor",
        "us_scan_analyze",
        "us_monitor",
        "reflect",
        "ml_train",
        "telegram_poll",
        "fills_sync",
    } <= set(jobs)
    assert jobs["kr_scan_analyze"].cron == {
        "hour": 7,
        "minute": 30,
        "day_of_week": "mon-fri",
        "timezone": "Asia/Seoul",
    }
    us = jobs["us_scan_analyze"].cron
    assert us["hour"] == 21 and us["minute"] == 30
    assert jobs["kr_monitor"].interval_s == 60 and jobs["telegram_poll"].interval_s == 30
    assert jobs["ml_train"].cron == {"hour": 10, "minute": 0, "day_of_week": "sat"}
    jobs_kr = {j.id for j in daemon.build_jobs(SCHED, Settings(markets=["KR"]), markets=["KR"])}
    assert "us_scan_analyze" not in jobs_kr


def test_run_job_logs_exception_without_raising(conn):
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("폭발")

    daemon.run_job("kr_scan", boom, conn_factory=lambda: db.connect())
    row = conn.execute("SELECT kind, status, error FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["kind"] == "kr_scan" and row["status"] == "error" and "폭발" in row["error"]
    daemon.run_job("kr_scan", lambda: {"ok": 1}, conn_factory=lambda: db.connect())
    row = conn.execute("SELECT status FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "ok"


def test_jobs_skip_holiday(conn):
    """휴장일에는 scan/analyze/monitor 잡 본체가 실행되지 않고 사유만 남는다."""
    market = FakeMarket(holidays={date(2026, 10, 9)})
    ran = []
    deps = _deps(conn, market, now=lambda: datetime(2026, 10, 9, 0, 0, tzinfo=UTC))
    r = daemon.market_job(deps, "KR", "scan", lambda: ran.append("scan"))
    assert r == {"skipped": "휴장일"} and ran == []
    deps.now = lambda: datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
    r = daemon.market_job(deps, "KR", "scan", lambda: ran.append("scan"))
    assert r is None and ran == ["scan"]


def test_monitor_job_runs_only_in_window(conn):
    ran = []
    deps = _deps(
        conn, FakeMarket(), now=lambda: datetime(2026, 10, 8, 3, 0, tzinfo=UTC)
    )  # 12:00 KST
    assert daemon.monitor_job(deps, "KR", lambda: ran.append(1)) is None and ran == [1]
    deps.now = lambda: datetime(2026, 10, 8, 10, 0, tzinfo=UTC)  # 19:00 KST
    assert daemon.monitor_job(deps, "KR", lambda: ran.append(1)) == {"skipped": "창 밖"}
    deps.now = lambda: datetime(2026, 10, 8, 16, 0, tzinfo=UTC)  # 01:00 KST → US 창 안
    assert daemon.monitor_job(deps, "US", lambda: ran.append(2)) is None and ran == [1, 2]


def _deps(conn, market, *, now):
    acct = make_account("paper-main", "paper", "auto", ["KR", "US"])
    cash = {"KR": Cash(currency="KRW", available=Decimal(1), total=Decimal(1))}
    return daemon.DaemonDeps(
        settings=Settings(),
        schedule=SCHED,
        accounts=[acct],
        brokers={"paper-main": FakeBroker(acct, cash=cash)},
        market_client=market,
        conn_factory=lambda: db.connect(),
        now=now,
    )


class FakeScheduler:
    def __init__(self):
        self.jobs = []
        self.started = False
        self.listeners = []

    def add_job(self, fn, trigger, **kw):
        self.jobs.append((kw.get("id"), trigger, fn))

    def add_listener(self, cb, mask):
        self.listeners.append((cb, mask))

    def get_jobs(self):
        return [SimpleNamespace(id=j[0], next_run_time=None) for j in self.jobs]

    def get_job(self, job_id):
        return SimpleNamespace(id=job_id, next_run_time=datetime(2026, 10, 23, 7, 30, tzinfo=UTC))

    def start(self):
        self.started = True

    def shutdown(self, wait=False):
        pass


def test_daemon_run_registers_jobs_with_fake_scheduler(project, conn):
    sched = FakeScheduler()
    deps = _deps(conn, FakeMarket(), now=lambda: datetime.now(UTC))
    r = daemon.run(deps, web=False, scheduler_factory=lambda: sched)
    assert sched.started and r["jobs"] >= 8
    ids = {j[0] for j in sched.jobs}
    assert {"kr_scan_analyze", "us_monitor", "telegram_poll"} <= ids
    assert not (project / "state" / "daemon.lock").exists()  # start 반환 후 해제


def test_daemon_run_writes_heartbeat_and_listener_sets_next(project, conn):
    import json

    from trader import heartbeat

    sched = FakeScheduler()
    deps = _deps(conn, FakeMarket(), now=lambda: datetime.now(UTC))
    daemon.run(deps, web=False, scheduler_factory=lambda: sched)
    p = project / "state" / "daemon_heartbeat.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid() and "kr_monitor" in data["jobs"] and data["stopped_at"]
    # 리스너: 스케줄러 시작 이벤트 → 모든 잡의 next_run, 잡 이벤트 → 그 잡의 next_run·missed
    assert len(sched.listeners) == 2
    on_started, on_job = sched.listeners[0][0], sched.listeners[1][0]
    on_started(SimpleNamespace(code=1))
    from apscheduler import events as ev

    on_job(SimpleNamespace(code=ev.EVENT_JOB_MISSED, job_id="kr_scan_analyze"))
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["jobs"]["kr_scan_analyze"]["last_status"] == "missed"
    assert data["jobs"]["kr_scan_analyze"]["next_run"] == "2026-10-23T07:30:00+00:00"
    st = heartbeat.read_status(
        project / "state",
        datetime.now(UTC),
        daemon.build_jobs(SCHED, Settings(), markets=["KR", "US"]),
    )
    assert st["alive"] is False and any(j["id"] == "kr_monitor" for j in st["jobs"])


def test_run_job_records_heartbeat_for_interval_jobs(conn, tmp_path):
    from trader import heartbeat

    hb = heartbeat.Heartbeat(tmp_path / "hb.json")
    daemon.run_job(
        "kr_monitor",
        lambda: {"skipped": "closed"},
        conn_factory=lambda: db.connect(),
        interval=True,
        heartbeat=hb,
    )
    daemon.run_job(
        "kr_monitor",
        lambda: {"error": "x"},
        conn_factory=lambda: db.connect(),
        interval=True,
        heartbeat=hb,
    )
    import json

    data = json.loads((tmp_path / "hb.json").read_text(encoding="utf-8"))
    assert (
        data["jobs"]["kr_monitor"]["last_status"] == "error"
        and data["jobs"]["kr_monitor"]["last_error"] == "x"
    )
    daemon.run_job(
        "kr_monitor",
        lambda: {"skipped": "closed"},
        conn_factory=lambda: db.connect(),
        interval=True,
        heartbeat=hb,
    )
    data = json.loads((tmp_path / "hb.json").read_text(encoding="utf-8"))
    assert data["jobs"]["kr_monitor"]["last_status"] == "skipped"
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1  # runs 행 정책은 그대로


def test_daemon_run_refuses_when_locked(project, conn):
    lock = daemon.Lock(project / "state" / "daemon.lock")
    assert lock.acquire()
    deps = _deps(conn, FakeMarket(), now=lambda: datetime.now(UTC))
    r = daemon.run(deps, web=False, scheduler_factory=FakeScheduler)
    assert r["ok"] is False and "잠금" in r["error"]
    lock.release()


def test_signal_handler_shuts_down_and_exits():
    import pytest

    sched = FakeScheduler()
    calls = []
    sched.shutdown = lambda wait=False: calls.append(wait)
    handler = daemon.make_signal_handler(sched)
    with pytest.raises(SystemExit):
        handler(15, None)
    # 실행 중인 잡(analyze 세션)이 끝나길 기다린 뒤 종료해야 잠금 해제가 세션보다 늦다
    assert calls == [True]


def test_run_names_jobs_by_id(project, conn):
    class NamedScheduler(FakeScheduler):
        def add_job(self, fn, trigger, **kw):
            self.jobs.append((kw.get("id"), kw.get("name"), fn))

    sched = NamedScheduler()
    deps = _deps(conn, FakeMarket(), now=lambda: datetime.now(UTC))
    daemon.run(deps, web=False, scheduler_factory=lambda: sched)
    assert all(j[0] == j[1] for j in sched.jobs)


# --- 리뷰 반영 ---------------------------------------------------------------------


def test_analyze_body_one_session_per_target_continues_on_failure(project, conn):
    from trader.orchestrator.run_store import write_checkpoint
    from trader.orchestrator.session import SessionResult

    deps = _deps(conn, FakeMarket(), now=lambda: datetime(2026, 10, 8, 0, 0, tzinfo=UTC))
    write_checkpoint(
        run_store.market_dir(date(2026, 10, 8), "KR", create=True),
        "_scan",
        {
            "analyze_targets": [
                {"ticker": "A", "exchange": None},
                {"ticker": "B", "exchange": None},
                {"ticker": "C", "exchange": None},
            ]
        },
    )
    seen = []

    def runner(spec, settings, conn_, accounts_, **kw):
        seen.append([t["ticker"] for t in spec.tickers])
        if spec.tickers[0]["ticker"] == "B":
            raise RuntimeError("세션 실패")
        return SessionResult(ok=True, session_id=f"s-{spec.tickers[0]['ticker']}", cost_usd=1.0)

    deps.session_runner = runner
    r = daemon._analyze_body(deps, "KR")
    assert seen == [["A"], ["B"], ["C"]]  # 종목당 세션 1회, 실패해도 계속
    assert r["ok"] == 2 and r["failed"] == ["B"]


def test_run_job_skips_runs_row_for_skipped_interval_job(conn):
    daemon.run_job(
        "kr_monitor", lambda: {"skipped": "창 밖"}, conn_factory=lambda: db.connect(), interval=True
    )
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    daemon.run_job(
        "kr_monitor", lambda: {"checked": 1}, conn_factory=lambda: db.connect(), interval=True
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    )  # interval 잡은 성공도 기록 안 함
    daemon.run_job("kr_scan", lambda: {"ok": 1}, conn_factory=lambda: db.connect())
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_should_skip_uses_market_timezone(conn):
    # KST 토요일 03:00 = 미국 금요일 14:00 ET → US 는 주말이 아니다
    sat_kst = datetime(2026, 10, 10, 3, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    assert daemon.should_skip("US", sat_kst, None, conn) is None
    assert daemon.should_skip("KR", sat_kst, FakeMarket(), conn) == "주말"
    sun_kst = datetime(2026, 10, 11, 12, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    assert daemon.should_skip("US", sun_kst, None, conn) == "주말"


def test_monitor_window_in_market_timezone(conn):
    sched = ScheduleConfig.model_validate(
        {
            **SCHED.model_dump(),
            "us": {
                "scan": "08:30",
                "analyze": "08:40",
                "monitor": {"start": "09:30", "end": "16:00", "interval_s": 60},
                "tz": "America/New_York",
            },
        }
    )
    deps = _deps(
        conn, FakeMarket(), now=lambda: datetime(2026, 10, 8, 14, 0, tzinfo=UTC)
    )  # 10:00 EDT
    deps.schedule = sched
    ran = []
    assert daemon.monitor_job(deps, "US", lambda: ran.append(1)) is None and ran == [1]
    deps.now = lambda: datetime(2026, 10, 8, 21, 0, tzinfo=UTC)  # 17:00 EDT
    assert daemon.monitor_job(deps, "US", lambda: ran.append(1)) == {"skipped": "창 밖"}
    jobs = {j.id: j for j in daemon.build_jobs(sched, Settings(), markets=["US"])}
    assert jobs["us_scan_analyze"].cron["timezone"] == "America/New_York"


def test_monitor_body_without_market_client_skips(conn):
    deps = _deps(conn, None, now=lambda: datetime(2026, 10, 8, 3, 0, tzinfo=UTC))
    assert daemon._monitor_body(deps, "KR") == {"skipped": "시세 클라이언트 없음"}


def test_daemon_ml_train_job_calls_train(conn, monkeypatch):
    from trader import daemon as d
    from trader.ml import train as train_mod
    from trader.ml.rl import train as rl_train_mod

    calls = []
    monkeypatch.setattr(
        train_mod, "train_quant", lambda cfg, **kw: calls.append(kw) or {"status": "trained"}
    )
    # RL 학습은 enabled 와 무관하게 늘 시도하고, 데이터 부족이면 알림 없이 결과만 붙인다
    monkeypatch.setattr(
        rl_train_mod, "train_sizer", lambda *a, **k: {"status": "insufficient_data"}
    )
    deps = _deps(conn, FakeMarket(), now=lambda: datetime(2026, 10, 10, 1, 0, tzinfo=UTC))
    job = next(j for j in d.build_jobs(SCHED, Settings(), markets=["KR"]) if j.id == "ml_train")
    r = d.job_body(deps, job)()
    assert r["status"] == "trained" and calls and calls[0]["state"] is None
    assert r["rl_sizer"] == {"status": "insufficient_data"}


def test_ml_train_body_wraps_exceptions(conn, monkeypatch):
    from trader import daemon as d
    from trader.ml import train as train_mod

    def boom(cfg, **kw):
        raise RuntimeError("학습 폭발")

    monkeypatch.setattr(train_mod, "train_quant", boom)
    deps = _deps(conn, FakeMarket(), now=lambda: datetime(2026, 10, 10, 1, 0, tzinfo=UTC))
    sent = []
    deps.notifier = type("N", (), {"send": lambda self, t, **k: sent.append(t) or True})()
    r = d._ml_train_body(deps)
    assert r["status"] == "error" and any("학습 폭발" in m for m in sent)


def test_daemon_reflect_job_skips_when_nothing_matured(conn, monkeypatch):
    from trader import daemon as d

    deps = _deps(conn, FakeMarket(), now=lambda: datetime(2026, 10, 10, 8, 0, tzinfo=UTC))
    job = next(j for j in d.build_jobs(SCHED, Settings(), markets=["KR"]) if j.id == "reflect")
    r = d.job_body(deps, job)()
    assert r["matured"] == 0 and r["sessions"] == 0


def test_us_holiday_skipped(conn):
    """US 휴장일은 정적 NYSE 달력(2026~2027)으로 건너뛴다 (Phase 6 이연)."""
    assert daemon.should_skip("US", date(2026, 12, 25), None, conn) == "휴장일"
    assert daemon.should_skip("US", date(2026, 11, 26), None, conn) == "휴장일"
    assert daemon.should_skip("US", date(2026, 7, 3), None, conn) == "휴장일"  # 7/4 토요일 대체
    assert daemon.should_skip("US", date(2027, 6, 18), None, conn) == "휴장일"
    assert daemon.should_skip("US", date(2026, 12, 24), None, conn) is None
    assert daemon.should_skip("KR", date(2026, 12, 25), None, conn) is None  # KR 은 KIS 조회
    assert len(daemon.us_holidays(2026) | daemon.us_holidays(2027)) == 20


def test_reflect_job_snapshots_features_first(conn):
    calls = []
    deps = _deps(conn, FakeMarket(), now=lambda: datetime(2026, 10, 8, 7, 0, tzinfo=UTC))
    deps.snapshot_fn = lambda markets: calls.append(list(markets)) or 3
    r = daemon._reflect_body(deps)
    assert calls == [["KR", "US"]] and r["features"] == 3

    class Sent:
        msgs = []

        def send(self, text, *, escape=True):
            self.msgs.append(text)
            return True

    deps.notifier = Sent()

    def boom(markets):
        raise RuntimeError("KIS 장애")

    deps.snapshot_fn = boom
    r = daemon._reflect_body(deps)  # 스냅샷 실패는 통보하고 반성은 계속
    assert r["features"] is None and any("피처 스냅샷 실패" in m for m in Sent.msgs)


def test_ml_train_job_trains_sizer_even_when_not_enabled(conn, monkeypatch):
    from trader import daemon as d
    from trader.config import MlConfig
    from trader.ml import train as train_mod
    from trader.ml.rl import train as rl_train_mod

    calls = []
    monkeypatch.setattr(
        train_mod,
        "train_quant",
        lambda *a, **k: {"status": "trained", "metrics": {}, "promoted": False},
    )
    monkeypatch.setattr(
        rl_train_mod,
        "train_sizer",
        lambda *a, **k: calls.append(1) or {"status": "trained", "version": "v", "summary": {}},
    )
    monkeypatch.setattr(
        "trader.config.load_ml",
        lambda: MlConfig.model_validate({"models": {"rl_sizer": {"enabled": False}}}),
    )
    deps = _deps(conn, FakeMarket(), now=lambda: datetime(2026, 10, 10, 1, 0, tzinfo=UTC))
    r = d._ml_train_body(deps)
    assert calls == [1] and r["rl_sizer"]["status"] == "trained"


# --- 이연 회수: 휴장일 규칙·stale running·reflect 시장별 격리 ---------------------------


def test_us_holidays_rules_match_static_and_2028():
    from trader import daemon as d

    assert d.us_holidays(2026) == {
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
        date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
        date(2026, 11, 26), date(2026, 12, 25),
    }  # fmt: skip
    assert d.us_holidays(2027) == {
        date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
        date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
        date(2027, 11, 25), date(2027, 12, 24),
    }  # fmt: skip
    h28 = d.us_holidays(2028)
    assert date(2028, 4, 14) in h28 and date(2028, 11, 23) in h28
    assert date(2027, 12, 31) not in h28 and date(2028, 1, 1) not in h28  # 토요일 신정: 대체 없음
    assert d.is_us_holiday(date(2028, 7, 4)) and not d.is_us_holiday(date(2028, 7, 5))
    assert d.should_skip("US", date(2028, 4, 14), None, None) == "휴장일"


def test_run_closes_stale_running_calls(conn, tmp_path):
    now = datetime(2026, 10, 10, 1, 0, tzinfo=UTC)
    for created in ("2026-10-09T20:00:00+00:00", "2026-10-10T00:50:00+00:00"):
        conn.execute(
            "INSERT INTO agent_calls(agent, status, created_at) "
            "VALUES ('orchestrator','running',?)",
            (created,),
        )
    conn.commit()
    deps = _deps(conn, FakeMarket(), now=lambda: now)
    daemon.run(deps, web=False, scheduler_factory=FakeScheduler, lock_path=tmp_path / "l.lock")
    rows = conn.execute("SELECT status, error FROM agent_calls ORDER BY id").fetchall()
    assert [r["status"] for r in rows] == ["error", "running"] and "stale" in rows[0]["error"]


def test_reflect_body_continues_after_market_error(conn, monkeypatch):
    from trader.orchestrator import reflect as reflect_mod
    from trader.orchestrator.session import PipelineError

    calls = []

    def fake_run_reflect(settings, c, accounts, *, market, **kw):
        calls.append(market)
        if market == "KR":
            raise PipelineError("세션 상한")
        return type("R", (), {"ok": True, "error": None, "session_id": "s", "fake": True})()

    monkeypatch.setattr(reflect_mod, "pending", lambda c, market: [1])
    monkeypatch.setattr(reflect_mod, "run_reflect", fake_run_reflect)
    sent = []
    deps = _deps(conn, FakeMarket(), now=lambda: datetime(2026, 10, 8, 7, 0, tzinfo=UTC))
    deps.notifier = type("N", (), {"send": lambda self, t, **k: sent.append(t) or True})()
    r = daemon._reflect_body(deps)
    assert calls == ["KR", "US"] and len(r["errors"]) == 1 and "KR" in r["errors"][0]
    assert any("reflect 실패" in m for m in sent)


def test_monitor_body_uses_market_run_date(conn, monkeypatch):
    """US 모니터는 KST 자정을 넘겨도 운영일(뉴욕 현지 날짜)을 넘긴다."""
    from zoneinfo import ZoneInfo

    from trader.orchestrator import monitor as monitor_mod

    seen = {}

    def fake_once(ctx, *, market, today):
        seen["today"] = today
        return {}

    monkeypatch.setattr(monitor_mod, "monitor_once", fake_once)
    kst = ZoneInfo("Asia/Seoul")
    deps = _deps(conn, FakeMarket(), now=lambda: datetime(2026, 10, 20, 0, 30, tzinfo=kst))
    daemon._monitor_body(deps, "US")
    assert seen["today"] == date(2026, 10, 19)
    daemon._monitor_body(deps, "KR")
    assert seen["today"] == date(2026, 10, 20)


def test_lock_acquire_is_atomic_against_concurrent_creation(project, monkeypatch):
    """exists→write 사이에 다른 데몬이 파일을 만들면 둘 다 잠금을 얻는다. O_EXCL 생성이어야 한다."""
    p = project / "state" / "daemon.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    real_link = os.link
    raced = {"done": False}

    def racing_link(src, dst, *a, **kw):
        if str(dst) == str(p) and not raced["done"]:
            raced["done"] = True
            p.write_text(json.dumps({"pid": os.getpid(), "started_at": "x"}), encoding="utf-8")
        return real_link(src, dst, *a, **kw)

    monkeypatch.setattr(daemon.os, "link", racing_link)
    assert daemon.Lock(p).acquire() is False  # 살아 있는 pid 가 먼저 잡았다
    assert raced["done"]


def test_lock_fresh_empty_file_is_not_taken_over(project):
    """다른 데몬이 O_EXCL 로 만들고 아직 pid 를 못 쓴 빈 파일(방금 생성)은 살아 있는 잠금으로 본다.
    pid 0 으로 읽혀 '죽은 데몬' 으로 넘겨받으면 두 데몬이 함께 돈다."""
    p = project / "state" / "daemon.lock"
    p.write_text("", encoding="utf-8")
    assert daemon.Lock(p).acquire() is False
    assert p.read_text(encoding="utf-8") == ""  # 남의 잠금을 건드리지 않았다


def test_lock_old_empty_file_is_taken_over(project):
    """오래된 빈 잠금(죽은 데몬이 쓰다 만 것)은 넘겨받는다."""
    import time

    p = project / "state" / "daemon.lock"
    p.write_text("", encoding="utf-8")
    old = time.time() - 3600
    os.utime(p, (old, old))
    assert daemon.Lock(p).acquire() is True
    assert json.loads(p.read_text(encoding="utf-8"))["pid"] == os.getpid()


def test_lock_file_appears_with_content_atomically(project, monkeypatch):
    """잠금 파일은 내용이 채워진 채로 나타나야 한다 (생성→쓰기 두 단계 금지)."""
    p = project / "state" / "daemon.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    seen: list[str] = []
    real_link = os.link

    def spying_link(src, dst, *a, **kw):
        real_link(src, dst, *a, **kw)
        seen.append(open(dst, encoding="utf-8").read())

    monkeypatch.setattr(daemon.os, "link", spying_link)
    assert daemon.Lock(p).acquire() is True
    assert seen and json.loads(seen[0])["pid"] == os.getpid()
    assert not list(p.parent.glob("daemon.lock.*.tmp"))  # 임시 파일은 남지 않는다


def test_signal_handler_ignores_reentrant_second_signal():
    """shutdown(wait=True) 중 두 번째 SIGINT 가 들어와도 SystemExit 로 대기를 끊고 잠금을
    먼저 풀면 안 된다 (잡 스레드가 아직 주문을 내는 중일 수 있다)."""
    import pytest

    sched = FakeScheduler()
    calls = []
    handler = daemon.make_signal_handler(sched)

    def shutdown(wait=False):
        calls.append(wait)
        handler(2, None)  # 대기 중 재진입: 무시돼야 한다 (예외도, 두 번째 shutdown 도 없음)

    sched.shutdown = shutdown
    with pytest.raises(SystemExit):
        handler(15, None)
    assert calls == [True]


def test_run_job_records_error_when_body_returns_error_dict(conn):
    body = lambda: {"targets": 0, "error": "_scan 없음"}  # noqa: E731
    r = daemon.run_job("kr_analyze", body, conn_factory=lambda: db.connect())
    assert r["error"] == "_scan 없음"
    row = conn.execute("SELECT status, error FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "error" and "_scan" in row["error"]


def test_analyze_body_without_scan_notifies_and_returns_error(project, conn):
    """_scan.json 이 없으면(scan 잡 지연·실패) 조용히 ok 로 끝나지 않고 알림 + error 로 남긴다."""

    class Sent:
        def __init__(self):
            self.msgs = []

        def send(self, text, *, escape=True):
            self.msgs.append(text)
            return True

    deps = _deps(conn, FakeMarket(), now=lambda: datetime(2026, 10, 8, 0, 0, tzinfo=UTC))
    deps.notifier = Sent()
    deps.session_runner = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("세션 금지"))
    r = daemon._analyze_body(deps, "KR")
    assert r["targets"] == 0 and "_scan" in r["error"]
    assert any("_scan" in m for m in deps.notifier.msgs)


def test_build_jobs_merges_scan_and_analyze_into_one_job():
    """scan 과 analyze 는 한 잡으로 이어서 돈다 (10분 간격 cron 두 개면 scan 이 늦을 때 analyze 가
    _scan.json 없이 떠서 하루 분석이 빠진다; 인라인 scan 은 screener 세션 이중 실행 위험)."""
    jobs = {j.id: j for j in daemon.build_jobs(SCHED, Settings(), markets=["KR", "US"])}
    assert "kr_scan_analyze" in jobs and "us_scan_analyze" in jobs
    assert "kr_analyze" not in jobs and "kr_scan" not in jobs
    assert jobs["kr_scan_analyze"].kind == "scan_analyze"
    kr = jobs["kr_scan_analyze"].cron
    assert kr["hour"] == 7 and kr["minute"] == 30
    # Phase 21: 시장별 야간 KIS 대조 cron(기본 16:40 현지, 시장 tz)
    assert jobs["kr_reconcile"].kind == "reconcile" and jobs["kr_reconcile"].market == "KR"
    assert jobs["kr_reconcile"].cron == {
        "hour": 16,
        "minute": 40,
        "day_of_week": "mon-fri",
        "timezone": "Asia/Seoul",
    }
    assert jobs["us_reconcile"].cron["timezone"] == "Asia/Seoul"  # SCHED 의 us.tz 기본값


def test_scan_analyze_body_runs_scan_then_analyze(conn, monkeypatch):
    order = []

    def scan(deps, m):
        order.append(("scan", m))
        return {"ok": 1}

    def analyze(deps, m, **kw):
        order.append(("analyze", m))
        return {"targets": 2}

    monkeypatch.setattr(daemon, "_scan_body", scan)
    monkeypatch.setattr(daemon, "_analyze_body", analyze)
    monkeypatch.setattr(daemon, "_market_view_body", lambda d, m: {"skipped": "test"})
    deps = _deps(conn, FakeMarket(), now=lambda: datetime(2026, 10, 8, 0, 0, tzinfo=UTC))
    r = daemon._scan_analyze_body(deps, "KR")
    assert order == [("scan", "KR"), ("analyze", "KR")] and r["targets"] == 2


def test_scan_analyze_body_records_shadow_portfolios(conn, monkeypatch):
    """전체 검토 L1: analyze 뒤 오늘 _scan 후보·LLM 결정으로 섀도 포트폴리오 3개를 남긴다."""
    from trader.ledger import shadow
    from trader.orchestrator.run_store import write_checkpoint

    deps = _deps(conn, FakeMarket(), now=lambda: datetime(2026, 10, 8, 0, 0, tzinfo=UTC))
    write_checkpoint(
        run_store.market_dir(date(2026, 10, 8), "KR", create=True),
        "_scan",
        {"candidates": [{"ticker": "005930", "score": 0.5}], "analyze_targets": []},
    )
    monkeypatch.setattr(daemon, "_scan_body", lambda d, m: {})
    monkeypatch.setattr(daemon, "_analyze_body", lambda d, m, **kw: {"targets": 0})
    monkeypatch.setattr(daemon, "_market_view_body", lambda d, m: {"skipped": "test"})
    daemon._scan_analyze_body(deps, "KR")
    got = {r["strategy"]: r["weights"] for r in shadow.rows(conn, market="KR")}
    assert got == {"candidates_1n": {"005930": 1.0}, "quant_topn": {"005930": 1.0}, "llm": {}}


def test_shadow_record_skips_incomplete_analysis_and_fake(conn, monkeypatch):
    """분석 실패·중단, FAKE 세션은 섀도 관측으로 남기지 않는다(PR #33 리뷰)."""
    from trader.ledger import shadow
    from trader.orchestrator.run_store import write_checkpoint

    deps = _deps(conn, FakeMarket(), now=lambda: datetime(2026, 10, 8, 0, 0, tzinfo=UTC))
    write_checkpoint(
        run_store.market_dir(date(2026, 10, 8), "KR", create=True),
        "_scan",
        {"candidates": [{"ticker": "005930", "score": 0.5}], "analyze_targets": []},
    )
    monkeypatch.delenv("FAKE_CLAUDE", raising=False)
    daemon._shadow_record(deps, "KR", {"targets": 2, "ok": 1, "failed": ["000660"]})
    daemon._shadow_record(deps, "KR", {"targets": 2, "ok": 0, "failed": [], "stopped": True})
    assert shadow.rows(conn, market="KR") == []
    monkeypatch.setenv("FAKE_CLAUDE", "1")
    daemon._shadow_record(deps, "KR", {"targets": 1, "ok": 1, "failed": []})
    assert shadow.rows(conn, market="KR") == []
    monkeypatch.delenv("FAKE_CLAUDE")
    daemon._shadow_record(deps, "KR", {"targets": 1, "ok": 1, "failed": []})
    assert len(shadow.rows(conn, market="KR")) == 3


def test_scan_analyze_body_scan_failure_still_runs_analyze_and_notifies(conn, monkeypatch):
    """scan 이 예외로 죽어도 analyze 는 돈다 (보유 재분석은 _scan 없이는 못 하지만 _analyze_body 가
    알림·error 를 남긴다). scan 실패도 통보한다."""

    class Sent:
        def __init__(self):
            self.msgs = []

        def send(self, text, *, escape=True):
            self.msgs.append(text)
            return True

    def boom(deps, m):
        raise RuntimeError("screener 실패")

    called = []
    monkeypatch.setattr(daemon, "_scan_body", boom)

    def analyze(deps, m, **kw):
        called.append(m)
        return {"targets": 0, "error": "_scan 없음"}

    monkeypatch.setattr(daemon, "_analyze_body", analyze)
    monkeypatch.setattr(daemon, "_market_view_body", lambda d, m: {"skipped": "test"})
    deps = _deps(conn, FakeMarket(), now=lambda: datetime(2026, 10, 8, 0, 0, tzinfo=UTC))
    deps.notifier = Sent()
    r = daemon._scan_analyze_body(deps, "KR")
    assert called == ["KR"] and r["error"]  # 알림은 _analyze_body 가 원인과 함께 1건만


def test_scan_analyze_job_closes_stale_running_before_sessions(conn, monkeypatch):
    """1시간 넘게 running 인 agent_calls 는 기동 때만이 아니라 매일 scan·analyze 앞에서도 정리한다
    (낮에 죽은 세션이 다음 재기동까지 running 으로 남아 상태 표시·오류율을 흐린다)."""
    now = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
    conn.execute(
        "INSERT INTO agent_calls(agent, status, created_at) VALUES ('orchestrator','running',?)",
        ((now - timedelta(hours=2)).isoformat(),),
    )
    conn.commit()
    monkeypatch.setattr(daemon, "_scan_body", lambda deps, m: {"ok": 1})
    monkeypatch.setattr(daemon, "_analyze_body", lambda deps, m, **kw: {"targets": 0})
    monkeypatch.setattr(daemon, "_market_view_body", lambda d, m: {"skipped": "test"})
    deps = _deps(conn, FakeMarket(), now=lambda: now)
    daemon._scan_analyze_body(deps, "KR")
    row = conn.execute("SELECT status FROM agent_calls").fetchone()
    assert row["status"] == "error"


def test_analyze_body_stops_launching_sessions_after_shutdown_signal(project, conn):
    """SIGTERM 뒤 shutdown(wait=True) 가 잡을 기다리는 동안 남은 종목 세션을 계속 띄우면
    launchd ExitTimeOut(세션 1회 기준) 안에 못 끝나 SIGKILL 된다 → 종료 플래그를 보고 멈춘다."""
    from trader.orchestrator.run_store import write_checkpoint
    from trader.orchestrator.session import SessionResult

    deps = _deps(conn, FakeMarket(), now=lambda: datetime(2026, 10, 8, 0, 0, tzinfo=UTC))
    write_checkpoint(
        run_store.market_dir(date(2026, 10, 8), "KR", create=True),
        "_scan",
        {"analyze_targets": [{"ticker": t, "exchange": None} for t in ("A", "B", "C")]},
    )
    seen = []

    def runner(spec, settings, conn_, accounts_, **kw):
        seen.append(spec.tickers[0]["ticker"])
        handler = daemon.make_signal_handler(FakeScheduler(), deps.stop_event)
        try:
            handler(15, None)  # 첫 세션 도중 SIGTERM
        except SystemExit:
            pass
        return SessionResult(ok=True, session_id="s", cost_usd=1.0)

    deps.session_runner = runner
    r = daemon._analyze_body(deps, "KR")
    assert seen == ["A"] and r["ok"] == 1 and r.get("stopped") is True
    assert deps.stop_event.is_set()


def test_scan_failure_is_reported_once_with_cause(conn, monkeypatch):
    """scan 예외와 그로 인한 '_scan 없음' 이 알림 두 번·error 두 번이 되면 안 된다. 원인 하나."""

    class Sent:
        def __init__(self):
            self.msgs = []

        def send(self, text, *, escape=True):
            self.msgs.append(text)
            return True

    def boom(deps, m):
        raise ValueError("screener 폭발")

    monkeypatch.setattr(daemon, "_scan_body", boom)
    monkeypatch.setattr(daemon, "_market_view_body", lambda d, m: {"skipped": "test"})
    deps = _deps(conn, FakeMarket(), now=lambda: datetime(2026, 10, 8, 0, 0, tzinfo=UTC))
    deps.notifier = Sent()
    r = daemon._scan_analyze_body(deps, "KR")
    assert len(deps.notifier.msgs) == 1 and "screener 폭발" in deps.notifier.msgs[0]
    assert "screener 폭발" in r["error"]


def test_schedule_analyze_key_is_optional():
    """analyze 시각은 더 쓰지 않으므로 yaml 에서 지워도 로드돼야 한다."""
    data = SCHED.model_dump()
    data["kr"].pop("analyze")
    data["us"].pop("analyze")
    sched = ScheduleConfig.model_validate(data)
    assert sched.kr.analyze is None and not hasattr(sched.kr, "analyze_hm")
    jobs = {j.id for j in daemon.build_jobs(sched, Settings(), markets=["KR"])}
    assert "kr_scan_analyze" in jobs


def test_daemon_run_skips_signals_when_asked(project, conn, monkeypatch):
    """트레이 모드는 데몬을 비메인 스레드에서 돌리므로 시그널 설치를 호출자가 맡는다."""
    called = []
    monkeypatch.setattr(daemon, "_install_signals", lambda *a, **k: called.append(a))
    deps = _deps(conn, FakeMarket(), now=lambda: datetime.now(UTC))
    daemon.run(deps, web=False, scheduler_factory=FakeScheduler, install_signals=False)
    assert called == []
    daemon.run(deps, web=False, scheduler_factory=FakeScheduler)
    assert len(called) == 1


def test_lock_with_reused_pid_is_taken_over(project):
    """재부팅 뒤 다른 프로세스가 잠금의 pid 를 받아도 시작 시각 식별자가 다르면 죽은 잠금으로
    본다."""
    p = project / "state" / "daemon.lock"
    p.write_text(
        json.dumps({"pid": os.getpid(), "started_at": "x", "proc_start": "not-this-process"}),
        encoding="utf-8",
    )
    assert daemon.Lock(p).acquire() is True
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()
    from trader.platform.procs import process_start

    assert data["proc_start"] == process_start(os.getpid())


def test_heartbeat_status_not_alive_when_lock_pid_is_another_process(project):
    from trader import heartbeat

    (project / "state").mkdir(exist_ok=True)
    (project / "state" / "daemon.lock").write_text(
        json.dumps({"pid": os.getpid(), "proc_start": "other"}), encoding="utf-8"
    )
    st = heartbeat.read_status(project / "state", datetime.now(UTC), [])
    assert st["alive"] is False


def test_analyze_targets_collect_right_before_each_session(conn, monkeypatch):
    """수집은 종목 세션 **직전**에 — 전 종목을 먼저 모으면 마지막 종목 입력이 늙는다(리뷰 F26)."""
    from types import SimpleNamespace as NS

    from trader.orchestrator.session import SessionResult

    order: list[str] = []
    deps = _deps(conn, FakeMarket(), now=lambda: datetime(2026, 10, 8, 0, 0, tzinfo=UTC))
    deps.input_deps = NS()
    deps.sources_cfg = NS()
    monkeypatch.setattr(
        "trader.sources.inputs.collect_and_save",
        lambda **kw: order.append(f"collect:{kw['ticker']}"),
    )
    monkeypatch.setattr(daemon, "_ensure_quant", lambda *a, **k: None)

    def runner(spec, *a, **kw):
        order.append(f"session:{spec.tickers[0]['ticker']}")
        return SessionResult(ok=True, session_id="s", fake=True)

    deps.session_runner = runner
    r = daemon._analyze_targets(
        deps, "KR", [{"ticker": "A"}, {"ticker": "B"}], today=date(2026, 10, 8)
    )
    assert r["ok"] == 2
    assert order == ["collect:A", "session:A", "collect:B", "session:B"]


def test_reconcile_body_runs_all_accounts_for_market(conn, monkeypatch):
    """Phase 21: reconcile 잡은 그 시장의 모든 계좌를 대조하고 결과를 계좌:시장 키로 돌려준다."""
    deps = _deps(conn, FakeMarket(), now=lambda: datetime(2026, 10, 8, 7, 40, tzinfo=UTC))
    out = daemon._reconcile_body(deps, "KR")
    assert list(out) == ["paper-main:KR"] and out["paper-main:KR"]["ok"] is True
    body = daemon.queued_job_body(
        deps, {"kind": "reconcile", "params": {"market": None}, "id": 1}, lambda s: None
    )
    assert set(body()) == {"paper-main:KR", "paper-main:US"}


def test_scan_line_and_listed_discovery_snapshot(conn, monkeypatch):
    """Phase 21: scan 알림 한 줄에 picks·발굴 요약, 유니버스 밖 발굴 종목은 세션 앞에서 피처
    스냅샷 1회(`features_fn("snapshot", tickers=)`)."""
    from types import SimpleNamespace as NS

    from trader.orchestrator.session import SessionResult

    line = daemon.scan_line(
        {
            "market": "KR",
            "date": "2026-10-08",
            "picks": [{"ticker": "005930"}, {"ticker": "247540"}],
            "analyze_targets": [{"ticker": "005930"}, {"ticker": "247540"}, {"ticker": "000660"}],
            "discovery": {
                "candidates": [{"ticker": "247540", "tier": "listed"}],
                "unresolved": [{"name": "x"}],
            },
            "cap_hit": True,
        }
    )
    assert line == (
        "[스캔] KR 2026-10-08: picks 2(005930, 247540) · 분석 대상 3 · "
        "뉴스 발굴 1(유니버스 0·밖 1·미해결 1) · 세션 상한"
    )

    calls: list = []
    deps = _deps(conn, FakeMarket(), now=lambda: datetime(2026, 10, 8, 0, 0, tzinfo=UTC))
    deps.input_deps = NS()
    deps.sources_cfg = NS()
    deps.features_fn = lambda action, markets, *, progress, **kw: calls.append(
        (action, markets, kw.get("tickers"))
    )
    monkeypatch.setattr("trader.sources.inputs.collect_and_save", lambda **kw: None)
    monkeypatch.setattr(daemon, "_ensure_quant", lambda *a, **k: None)
    deps.session_runner = lambda spec, *a, **kw: SessionResult(ok=True, session_id="s", fake=True)
    r = daemon._analyze_targets(
        deps,
        "KR",
        [{"ticker": "005930"}, {"ticker": "247540", "exchange": None, "discovery": "listed"}],
        today=date(2026, 10, 8),
    )
    assert r["ok"] == 2
    assert calls == [("snapshot", ["KR"], [("247540", None)])]
    # features_fn 이 없으면(시세 클라이언트 없음) 건너뛴다
    deps.features_fn = None
    calls.clear()
    daemon._analyze_targets(
        deps, "KR", [{"ticker": "247540", "discovery": "listed"}], today=date(2026, 10, 8)
    )
    assert calls == []


def test_analyze_body_skips_held_only_targets_by_horizon_profile(project, conn):
    """Phase 23 T: 보유 종목(source=held)은 최근 결정의 프로필 주기(장기 5운영일)마다만 아침 재분석.
    picks·결정 없는 보유분은 그대로."""
    from trader.orchestrator.run_store import write_checkpoint
    from trader.orchestrator.session import SessionResult

    today = datetime(2026, 10, 27, 0, 0, tzinfo=UTC)  # 월요일
    deps = _deps(conn, FakeMarket(), now=lambda: today)
    for ticker, hd in (("L", 60), ("S", 5), ("P", 60)):
        conn.execute(
            "INSERT INTO decisions(date, market, ticker, action, json, created_at) "
            "VALUES ('2026-10-24','KR',?,'BUY',?,'2026-10-24T00:00:00+00:00')",
            (ticker, json.dumps({"horizon_days": hd})),
        )
    conn.commit()
    write_checkpoint(
        run_store.market_dir(date(2026, 10, 27), "KR", create=True),
        "_scan",
        {
            "analyze_targets": [
                {"ticker": "P", "exchange": None, "source": "pick"},  # pick 은 항상
                {"ticker": "L", "exchange": None, "source": "held"},  # 장기, 1 평일 뒤 → 건너뜀
                {"ticker": "S", "exchange": None, "source": "held"},  # 단기(매일) → 분석
                {"ticker": "N", "exchange": None, "source": "held"},  # 결정 없음 → 분석
            ]
        },
    )
    seen = []

    def runner(spec, settings, conn_, accounts_, **kw):
        seen.append(spec.tickers[0]["ticker"])
        return SessionResult(ok=True, session_id=f"s-{spec.tickers[0]['ticker']}", cost_usd=1.0)

    deps.session_runner = runner
    r = daemon._analyze_body(deps, "KR")
    assert seen == ["P", "S", "N"]
    assert r["skipped_held"] == [
        {"ticker": "L", "profile": "long", "last": "2026-10-24", "every": 5}
    ]


def test_lock_os_hold_blocks_second_even_if_pidfile_removed(project):
    """전체 검토 P4: pid 파일이 지워져도(경합 중 상대가 unlink) OS 잠금을 쥔 데몬이 있으면 못 뜬다.
    해제하면 다시 잡을 수 있다."""
    p = project / "state" / "daemon.lock"
    a = daemon.Lock(p)
    assert a.acquire() is True
    p.unlink()  # 다른 프로세스가 '죽은 잠금' 으로 오판해 지운 상황
    assert daemon.Lock(p).acquire() is False
    a.release()
    b = daemon.Lock(p)
    assert b.acquire() is True
    b.release()
