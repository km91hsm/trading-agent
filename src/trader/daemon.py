"""데몬 (스펙 §10.1): APScheduler 로 schedule.yaml 을 실행한다.

- 단일 인스턴스: state/daemon.lock(pid·시작 시각). 죽은 pid 의 잠금은 넘겨받는다.
- 잡: 시장별 scan/analyze(cron)·monitor(interval, 창 안에서만), telegram_poll(30초), fills_sync,
  reflect(Phase 8 자리)·ml_train(Phase 7 자리). 모든 잡은 run_job 이 감싸 예외를 runs 에 남긴다.
- 휴장일·주말·paused 면 시장 잡을 건너뛰고 사유를 로그에 남긴다.
- web 서버는 같은 프로세스의 데몬 스레드로 띄운다.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from functools import partial
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trader.config import AccountConfig, ScheduleConfig, Settings
from trader.control import control
from trader.ledger import db
from trader.notify.telegram import Notifier, NullNotifier
from trader.platform import paths
from trader.platform.procs import pid_alive

logger = logging.getLogger("trader.daemon")
KST = ZoneInfo("Asia/Seoul")
TELEGRAM_POLL_S = 30
FILLS_SYNC_TIMES = ((16, 10), (7, 10))  # KR 마감 후, US 마감 후 (KST)


# --- 잠금 -----------------------------------------------------------------------


class Lock:
    def __init__(self, path: Path) -> None:
        self.path = path

    def acquire(self) -> bool:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                pid = int(data.get("pid", 0))
            except (ValueError, OSError):
                pid = 0
            if pid and pid_alive(pid):  # 같은 프로세스가 두 번 잡는 것도 거부한다
                logger.error("데몬이 이미 실행 중입니다 (pid %s): %s", pid, self.path)
                return False
            logger.warning("죽은 데몬의 잠금을 넘겨받습니다 (pid %s)", pid)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"pid": os.getpid(), "started_at": datetime.now(UTC).isoformat()}),
            encoding="utf-8",
        )
        return True

    def release(self) -> None:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if int(data.get("pid", 0)) == os.getpid():
                    self.path.unlink()
        except (ValueError, OSError):
            pass


# --- 창·건너뜀 ---------------------------------------------------------------------


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def in_window(now_t: time, start: str, end: str) -> bool:
    """start ≤ now ≤ end. end 가 start 보다 이르면 자정을 넘는 창이다 (US 23:30~06:00)."""
    s, e = _hm(start), _hm(end)
    if s <= e:
        return s <= now_t <= e
    return now_t >= s or now_t <= e


def should_skip(
    market: str, today: date, market_client: Any | None, conn: sqlite3.Connection | None
) -> str | None:
    if conn is not None and control.is_paused(conn):
        return "일시정지"
    if today.weekday() >= 5:
        return "주말"
    if market == "KR" and market_client is not None:
        try:
            if market_client.is_holiday(today):
                return "휴장일"
        except Exception as e:  # noqa: BLE001 - 휴장일 조회 실패는 개장으로 본다
            logger.warning("휴장일 조회 실패, 개장일로 간주: %s", e)
    return None


# --- 잡 정의 ------------------------------------------------------------------------


@dataclass
class Job:
    id: str
    kind: str  # scan | analyze | monitor | reflect | ml_train | telegram_poll | fills_sync
    market: str | None = None
    cron: dict | None = None
    interval_s: int | None = None


def build_jobs(schedule: ScheduleConfig, settings: Settings, *, markets: list[str]) -> list[Job]:
    jobs: list[Job] = []
    for m in markets:
        ms = schedule.kr if m == "KR" else schedule.us
        lo = m.lower()
        h, mi = ms.scan_hm()
        jobs.append(
            Job(f"{lo}_scan", "scan", m, cron={"hour": h, "minute": mi, "day_of_week": "mon-fri"})
        )
        h, mi = ms.analyze_hm()
        jobs.append(
            Job(
                f"{lo}_analyze",
                "analyze",
                m,
                cron={"hour": h, "minute": mi, "day_of_week": "mon-fri"},
            )
        )
        jobs.append(Job(f"{lo}_monitor", "monitor", m, interval_s=ms.monitor.interval_s))
    h, mi = _hm(schedule.reflect).hour, _hm(schedule.reflect).minute
    jobs.append(Job("reflect", "reflect", cron={"hour": h, "minute": mi, "day_of_week": "mon-fri"}))
    t = _hm(schedule.ml_train.time)
    jobs.append(
        Job(
            "ml_train",
            "ml_train",
            cron={"hour": t.hour, "minute": t.minute, "day_of_week": schedule.ml_train.weekday},
        )
    )
    jobs.append(Job("telegram_poll", "telegram_poll", interval_s=TELEGRAM_POLL_S))
    for i, (h, mi) in enumerate(FILLS_SYNC_TIMES):
        jobs.append(
            Job(
                "fills_sync" if i == 0 else f"fills_sync_{i}",
                "fills_sync",
                cron={"hour": h, "minute": mi, "day_of_week": "mon-sat"},
            )
        )
    return jobs


def run_job(
    name: str, fn: Callable[[], Any], *, conn_factory: Callable[[], sqlite3.Connection]
) -> Any:
    """잡 본체를 감싼다: runs 에 시작·종료 기록, 예외는 로그·error 행으로 남기고 삼킨다."""
    conn = conn_factory()
    try:
        run_id = db.start_run(conn, name, None)
    finally:
        conn.close()
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001 - 잡 하나의 실패가 데몬을 죽이지 않는다
        logger.exception("잡 실패 %s", name)
        conn = conn_factory()
        try:
            db.finish_run(conn, run_id, "error", f"{type(e).__name__}: {e}")
        finally:
            conn.close()
        return None
    conn = conn_factory()
    try:
        db.finish_run(conn, run_id, "ok", None)
    finally:
        conn.close()
    return result


# --- 의존성·잡 본체 -------------------------------------------------------------------


@dataclass
class DaemonDeps:
    settings: Settings
    schedule: ScheduleConfig
    accounts: list[AccountConfig]
    brokers: dict[str, Any]
    market_client: Any | None
    conn_factory: Callable[[], sqlite3.Connection]
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    notifier: Notifier = field(default_factory=NullNotifier)
    receiver: Any | None = None
    secrets: Any | None = None
    universe: Any | None = None
    sources_cfg: Any | None = None
    input_deps: Any | None = None
    state: Path | None = None
    web_app_factory: Callable[[], Any] | None = None
    dry_run: bool = False


def _today(deps: DaemonDeps) -> date:
    return deps.now().astimezone(KST).date()


def market_job(deps: DaemonDeps, market: str, kind: str, fn: Callable[[], Any]) -> dict | None:
    """시장 잡 공통 게이트: 주말·휴장일·paused 면 건너뛴다."""
    conn = deps.conn_factory()
    try:
        why = should_skip(market, _today(deps), deps.market_client, conn)
    finally:
        conn.close()
    if why:
        logger.info("%s %s 건너뜀: %s", market, kind, why)
        return {"skipped": why}
    fn()
    return None


def monitor_job(deps: DaemonDeps, market: str, fn: Callable[[], Any]) -> dict | None:
    ms = deps.schedule.kr if market == "KR" else deps.schedule.us
    now_kst = deps.now().astimezone(KST)
    if not in_window(
        now_kst.time().replace(second=0, microsecond=0), ms.monitor.start, ms.monitor.end
    ):
        return {"skipped": "창 밖"}
    return market_job(deps, market, "monitor", fn)


def _scan_body(deps: DaemonDeps, market: str) -> dict:
    from trader.config import load_universe
    from trader.orchestrator import tools
    from trader.orchestrator.run_store import write_checkpoint
    from trader.scan import quant, screener

    universe = deps.universe or load_universe()
    today = _today(deps)
    cands = quant.candidates(universe, market, state=deps.state)
    conn = deps.conn_factory()
    try:
        cap_hit = tools.sessions_today(conn, deps.now()) >= deps.settings.llm.daily_session_cap
        r = screener.run_screen(
            deps.settings,
            conn,
            deps.accounts,
            market=market,
            date=today,
            candidates=cands,
            cap_hit=cap_hit,
            state=deps.state,
            now=deps.now,
            brokers=deps.brokers,
            watch=list(universe.watch),
            asof=deps.now(),
        )
    finally:
        conn.close()
    out = {
        "market": market,
        "date": today.isoformat(),
        "candidates": cands,
        **r,
        "cap_hit": cap_hit,
    }
    write_checkpoint(screener.market_dir(today, market, deps.state), "_scan", out)
    return out


def _analyze_body(deps: DaemonDeps, market: str) -> dict:
    from trader.orchestrator.run_store import read_checkpoint
    from trader.orchestrator.session import SessionSpec, run_session
    from trader.scan import screener
    from trader.sources.inputs import collect_and_save

    today = _today(deps)
    scan = read_checkpoint(screener.market_dir(today, market, deps.state), "_scan") or {}
    targets = scan.get("analyze_targets") or []
    if not targets:
        logger.info("%s 분석 대상 없음 (_scan.json 없음 또는 빈 picks)", market)
        return {"targets": 0}
    if deps.input_deps is not None and deps.sources_cfg is not None:
        for t in targets:
            try:
                collect_and_save(
                    ticker=t["ticker"],
                    market=market,
                    exchange=t.get("exchange"),
                    asof=deps.now(),
                    cfg=deps.sources_cfg,
                    secrets=deps.secrets,
                    deps=deps.input_deps,
                    state=deps.state,
                )
            except Exception as e:  # noqa: BLE001 - 한 종목 수집 실패는 세션이 다시 시도한다
                logger.warning("수집 실패 %s %s: %s", market, t["ticker"], e)
    conn = deps.conn_factory()
    try:
        res = run_session(
            SessionSpec(
                market=market, date=today, tickers=targets, dry_run=deps.dry_run, asof=deps.now()
            ),
            deps.settings,
            conn,
            deps.accounts,
            state=deps.state,
            now=deps.now,
            brokers=deps.brokers,
            notifier=deps.notifier,
        )
    finally:
        conn.close()
    if not res.ok:
        raise RuntimeError(f"analyze 세션 실패: {res.error}")
    return {"targets": len(targets), "session_id": res.session_id, "cost_usd": res.cost_usd}


def _monitor_body(deps: DaemonDeps, market: str) -> dict:
    from trader.orchestrator import monitor as monitor_mod
    from trader.orchestrator.session import run_session

    conn = deps.conn_factory()
    try:
        ctx = monitor_mod.MonitorContext(
            settings=deps.settings,
            accounts=deps.accounts,
            conn=conn,
            brokers=deps.brokers,
            market_client=deps.market_client,
            notifier=deps.notifier,
            now=deps.now,
            session_runner=run_session,
            dry_run=deps.dry_run,
            state=deps.state,
            exchanges={u.symbol: u.exchange for u in (deps.universe.us if deps.universe else [])},
        )
        return monitor_mod.monitor_once(ctx, market=market, today=_today(deps))
    finally:
        conn.close()


def _telegram_body(deps: DaemonDeps) -> dict:
    from trader.notify.commands import poll_once
    from trader.orchestrator.executor import ExecContext

    if deps.receiver is None:
        return {"skipped": "텔레그램 미설정"}
    conn = deps.conn_factory()
    try:
        ectx = ExecContext(
            settings=deps.settings,
            accounts=deps.accounts,
            conn=conn,
            brokers=deps.brokers,
            notifier=deps.notifier,
            state=deps.state,
            now=deps.now,
        )
        return poll_once(ectx, deps.receiver, echo=logger.info)
    finally:
        conn.close()


def _fills_body(deps: DaemonDeps) -> dict:
    from trader.orchestrator.fills import sync_fills

    out = {}
    conn = deps.conn_factory()
    try:
        for a in deps.accounts:
            b = deps.brokers.get(a.id)
            if b is not None:
                out[a.id] = sync_fills(conn, b, a, _today(deps))
    finally:
        conn.close()
    return out


def job_body(deps: DaemonDeps, job: Job) -> Callable[[], Any]:
    if job.kind == "scan":
        return lambda: market_job(
            deps, job.market or "KR", "scan", lambda: _scan_body(deps, job.market or "KR")
        )
    if job.kind == "analyze":
        return lambda: market_job(
            deps, job.market or "KR", "analyze", lambda: _analyze_body(deps, job.market or "KR")
        )
    if job.kind == "monitor":
        return lambda: monitor_job(
            deps, job.market or "KR", lambda: _monitor_body(deps, job.market or "KR")
        )
    if job.kind == "telegram_poll":
        return lambda: _telegram_body(deps)
    if job.kind == "fills_sync":
        return lambda: _fills_body(deps)
    if job.kind == "reflect":
        return lambda: logger.info("reflect 는 Phase 8 에서 구현한다 (자리)")
    if job.kind == "ml_train":
        return lambda: logger.info("ml train 은 Phase 7 에서 구현한다 (자리)")
    raise ValueError(f"알 수 없는 잡: {job.kind}")


# --- 실행 ---------------------------------------------------------------------------


def _start_web(app: Any, host: str, port: int) -> threading.Thread:
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info"))
    t = threading.Thread(target=server.run, name="trader-web", daemon=True)
    t.start()
    return t


def run(
    deps: DaemonDeps,
    *,
    web: bool = True,
    scheduler_factory: Callable[[], Any] | None = None,
    lock_path: Path | None = None,
) -> dict:
    """잠금 → (web 스레드) → 잡 등록 → scheduler.start(). 반환은 start 가 끝난 뒤(종료 시)."""
    base = deps.state if deps.state is not None else paths.state_dir()
    lock = Lock(lock_path or base / "daemon.lock")
    if not lock.acquire():
        return {"ok": False, "error": "잠금 실패: 데몬이 이미 실행 중"}
    try:
        if scheduler_factory is None:
            from apscheduler.schedulers.blocking import BlockingScheduler

            scheduler = BlockingScheduler(timezone=KST)
        else:
            scheduler = scheduler_factory()
        if web and deps.web_app_factory is not None:
            _start_web(deps.web_app_factory(), deps.settings.web.bind, deps.settings.web.port)
        jobs = build_jobs(deps.schedule, deps.settings, markets=list(deps.settings.markets))
        for job in jobs:
            scheduler.add_job(
                partial(run_job, job.id, job_body(deps, job), conn_factory=deps.conn_factory),
                _trigger(job),
                id=job.id,
                name=job.id,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=120,
            )
        logger.info("데몬 시작: 잡 %d개, web=%s", len(jobs), web)
        _install_signals(scheduler)
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("데몬 종료")
        return {"ok": True, "jobs": len(jobs)}
    finally:
        lock.release()


def make_signal_handler(scheduler: Any) -> Callable[[int, Any], None]:
    """SIGINT/SIGTERM → 스케줄러 중단 후 SystemExit (finally 의 잠금 해제가 돌도록)."""

    def handler(signum: int, frame: Any) -> None:
        logger.info("시그널 %s 수신, 데몬을 종료합니다", signum)
        try:
            scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001 - 종료 중 오류는 무시
            pass
        raise SystemExit(0)

    return handler


def _install_signals(scheduler: Any) -> None:
    import signal

    handler = make_signal_handler(scheduler)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):  # 메인 스레드가 아니면 등록 불가
            pass


def _trigger(job: Job) -> Any:
    try:
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:  # pragma: no cover - 의존성 누락
        return job.cron or {"interval_s": job.interval_s}
    if job.cron:
        return CronTrigger(timezone=KST, **job.cron)
    return IntervalTrigger(seconds=int(job.interval_s or 60), timezone=KST)
