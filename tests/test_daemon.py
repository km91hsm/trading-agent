import json
import os
from datetime import UTC, date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

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
    assert jobs["kr_scan"].cron == {
        "hour": 7,
        "minute": 30,
        "day_of_week": "mon-fri",
        "timezone": "Asia/Seoul",
    }
    assert jobs["us_analyze"].cron["hour"] == 21 and jobs["us_analyze"].cron["minute"] == 40
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


def test_signal_handler_shuts_down_and_exits():
    import pytest

    sched = FakeScheduler()
    calls = []
    sched.shutdown = lambda wait=False: calls.append(wait)
    handler = daemon.make_signal_handler(sched)
    with pytest.raises(SystemExit):
        handler(15, None)
    assert calls == [False]


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
    from trader.scan import screener

    deps = _deps(conn, FakeMarket(), now=lambda: datetime(2026, 10, 8, 0, 0, tzinfo=UTC))
    write_checkpoint(
        screener.market_dir(date(2026, 10, 8), "KR"),
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
    assert jobs["us_scan"].cron["timezone"] == "America/New_York"


def test_monitor_body_without_market_client_skips(conn):
    deps = _deps(conn, None, now=lambda: datetime(2026, 10, 8, 3, 0, tzinfo=UTC))
    assert daemon._monitor_body(deps, "KR") == {"skipped": "시세 클라이언트 없음"}
