import json
import os
from datetime import UTC, date, datetime, time
from decimal import Decimal

from conftest import make_account

from trader import daemon
from trader.broker.fake import FakeBroker, FakeMarket
from trader.broker.models import Cash
from trader.config import ScheduleConfig, Settings
from trader.control import control
from trader.ledger import db

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
        "kr_scan",
        "kr_analyze",
        "kr_monitor",
        "us_scan",
        "us_analyze",
        "us_monitor",
        "reflect",
        "ml_train",
        "telegram_poll",
        "fills_sync",
    } <= set(jobs)
    assert jobs["kr_scan"].cron == {"hour": 7, "minute": 30, "day_of_week": "mon-fri"}
    assert jobs["us_analyze"].cron == {"hour": 21, "minute": 40, "day_of_week": "mon-fri"}
    assert jobs["kr_monitor"].interval_s == 60 and jobs["telegram_poll"].interval_s == 30
    assert jobs["ml_train"].cron == {"hour": 10, "minute": 0, "day_of_week": "sat"}
    jobs_kr = {j.id for j in daemon.build_jobs(SCHED, Settings(markets=["KR"]), markets=["KR"])}
    assert "us_scan" not in jobs_kr


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

    def add_job(self, fn, trigger, **kw):
        self.jobs.append((kw.get("id"), trigger, fn))

    def start(self):
        self.started = True

    def shutdown(self, wait=False):
        pass


def test_daemon_run_registers_jobs_with_fake_scheduler(project, conn):
    sched = FakeScheduler()
    deps = _deps(conn, FakeMarket(), now=lambda: datetime.now(UTC))
    r = daemon.run(deps, web=False, scheduler_factory=lambda: sched)
    assert sched.started and r["jobs"] >= 10
    ids = {j[0] for j in sched.jobs}
    assert {"kr_scan", "us_monitor", "telegram_poll"} <= ids
    assert not (project / "state" / "daemon.lock").exists()  # start 반환 후 해제


def test_daemon_run_refuses_when_locked(project, conn):
    lock = daemon.Lock(project / "state" / "daemon.lock")
    assert lock.acquire()
    deps = _deps(conn, FakeMarket(), now=lambda: datetime.now(UTC))
    r = daemon.run(deps, web=False, scheduler_factory=FakeScheduler)
    assert r["ok"] is False and "잠금" in r["error"]
    lock.release()
