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


MARKET_TZ = {"KR": ZoneInfo("Asia/Seoul"), "US": ZoneInfo("America/New_York")}


def market_date(market: str, now: datetime | date) -> date:
    """시장 현지 날짜 (주말 판정용). date 를 주면 그대로 쓴다."""
    if isinstance(now, datetime):
        return now.astimezone(MARKET_TZ.get(market, KST)).date()
    return now


def should_skip(
    market: str, today: datetime | date, market_client: Any | None, conn: sqlite3.Connection | None
) -> str | None:
    """paused·주말(시장 현지 날짜)·KR 휴장일(KIS). US 휴장일은 Phase 8(KIS 해외 휴장 API 미확인)."""
    if conn is not None and control.is_paused(conn):
        return "일시정지"
    local = market_date(market, today)
    if local.weekday() >= 5:
        return "주말"
    today = local
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
            Job(
                f"{lo}_scan",
                "scan",
                m,
                cron={"hour": h, "minute": mi, "day_of_week": "mon-fri", "timezone": ms.tz},
            )
        )
        h, mi = ms.analyze_hm()
        jobs.append(
            Job(
                f"{lo}_analyze",
                "analyze",
                m,
                cron={"hour": h, "minute": mi, "day_of_week": "mon-fri", "timezone": ms.tz},
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
    name: str,
    fn: Callable[[], Any],
    *,
    conn_factory: Callable[[], sqlite3.Connection],
    interval: bool = False,
) -> Any:
    """잡 본체를 감싼다. cron 잡은 runs 에 시작·종료를 남기고, interval 잡(monitor·텔레그램)은
    오류일 때만 남긴다(분 단위 행 폭주 방지). 예외는 로그·error 행으로 삼킨다."""
    run_id: int | None = None
    if not interval:
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
            if run_id is None:
                run_id = db.start_run(conn, name, None)
            db.finish_run(conn, run_id, "error", f"{type(e).__name__}: {e}")
        finally:
            conn.close()
        return None
    if run_id is not None:
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
    session_runner: Callable[..., Any] | None = None  # 테스트 주입용 (기본 run_session)


def _today(deps: DaemonDeps) -> date:
    return deps.now().astimezone(KST).date()


def market_job(deps: DaemonDeps, market: str, kind: str, fn: Callable[[], Any]) -> dict | None:
    """시장 잡 공통 게이트: 주말·휴장일·paused 면 건너뛴다."""
    conn = deps.conn_factory()
    try:
        why = should_skip(market, deps.now(), deps.market_client, conn)
    finally:
        conn.close()
    if why:
        logger.info("%s %s 건너뜀: %s", market, kind, why)
        return {"skipped": why}
    fn()
    return None


def monitor_job(deps: DaemonDeps, market: str, fn: Callable[[], Any]) -> dict | None:
    ms = deps.schedule.kr if market == "KR" else deps.schedule.us
    now_local = deps.now().astimezone(ZoneInfo(ms.tz))
    if not in_window(
        now_local.time().replace(second=0, microsecond=0), ms.monitor.start, ms.monitor.end
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
    from trader.ml import ranker

    ml = ranker.current(deps.state)
    cands = quant.candidates(universe, market, state=deps.state, rank_fn=ml[0] if ml else None)
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
        "ml_model": ml[1] if ml else None,
    }
    write_checkpoint(screener.market_dir(today, market, deps.state), "_scan", out)
    return out


def _ensure_quant(deps: DaemonDeps, ticker: str, market: str, today: date) -> None:
    from trader.orchestrator import tools

    conn = deps.conn_factory()
    try:
        ctx = tools.ToolContext(
            settings=deps.settings,
            accounts=deps.accounts,
            conn=conn,
            state=deps.state,
            now=deps.now,
        )
        tools.ensure_quant(ctx, ticker=ticker, market=market, date=today)  # type: ignore[arg-type]
        tools.ensure_calibration(ctx, ticker=ticker, market=market, date=today)  # type: ignore[arg-type]
    except Exception as e:  # noqa: BLE001 - 05_quant 는 선택 입력
        logger.warning("05_quant 생성 실패 %s %s: %s", market, ticker, e)
    finally:
        conn.close()


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
            _ensure_quant(deps, t["ticker"], market, today)
    # 종목당 세션 1회(--max-budget-usd 는 세션 단위). 실패한 종목은 기록하고 다음 종목으로.
    runner = deps.session_runner or run_session
    ok, failed, sessions = 0, [], []
    for t in targets:
        conn = deps.conn_factory()
        try:
            res = runner(
                SessionSpec(
                    market=market, date=today, tickers=[t], dry_run=deps.dry_run, asof=deps.now()
                ),
                deps.settings,
                conn,
                deps.accounts,
                state=deps.state,
                now=deps.now,
                brokers=deps.brokers,
                notifier=deps.notifier,
            )
        except Exception as e:  # noqa: BLE001 - 한 종목 세션 실패가 나머지를 막지 않는다
            logger.exception("analyze 세션 기동 실패 %s %s", market, t["ticker"])
            failed.append(t["ticker"])
            deps.notifier.send(f"[analyze 실패] {market} {t['ticker']}: {type(e).__name__}: {e}")
            continue
        finally:
            conn.close()
        if res.ok:
            ok += 1
            sessions.append(
                {"ticker": t["ticker"], "session_id": res.session_id, "cost_usd": res.cost_usd}
            )
        else:
            failed.append(t["ticker"])
            deps.notifier.send(f"[analyze 실패] {market} {t['ticker']}: {res.error}")
    return {"targets": len(targets), "ok": ok, "failed": failed, "sessions": sessions}


def _monitor_body(deps: DaemonDeps, market: str) -> dict:
    from trader.orchestrator import monitor as monitor_mod
    from trader.orchestrator.session import run_session

    if deps.market_client is None:
        return {"skipped": "시세 클라이언트 없음"}
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
            session_runner=deps.session_runner or run_session,
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


def _reflect_body(deps: DaemonDeps) -> dict:
    """장 마감 후: 만기 결정 결과 채우기 → 반성 대기가 있는 시장만 세션 → 메모리 정리."""
    from trader.memory import outcomes, store
    from trader.orchestrator.reflect import pending, run_reflect

    conn = deps.conn_factory()
    try:
        matured = outcomes.mature(conn, state=deps.state, today=_today(deps))
        sessions = 0
        for market in deps.settings.markets:
            if not pending(conn, market=market):
                continue
            res = run_reflect(
                deps.settings,
                conn,
                deps.accounts,
                market=market,
                date=_today(deps),
                state=deps.state,
                now=deps.now,
                dry_run=True,
                brokers=deps.brokers,
                notifier=deps.notifier,
            )
            if res.session_id or res.fake:
                sessions += 1
            if not res.ok:
                deps.notifier.send(f"[reflect 실패] {market}: {res.error}")
        pr = store.promote_and_prune(conn, now=deps.now(), settings=deps.settings.memory)
    finally:
        conn.close()
    return {"matured": len(matured), "sessions": sessions, "pruned": pr["deleted"]}


def _ml_train_body(deps: DaemonDeps) -> dict:
    from trader.config import load_ml
    from trader.ml import train as train_mod

    try:
        r = train_mod.train_quant(load_ml(), state=deps.state, now=deps.now)
    except Exception as e:  # noqa: BLE001 - 학습 실패는 통보하고 데몬은 계속
        logger.exception("ML 학습 실패")
        deps.notifier.send(f"[ML 학습 실패] {type(e).__name__}: {e}")
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    met = r.get("metrics") or {}
    acc = (
        f"정확도 {met['accuracy']:.3f} / 기준선 {met['baseline']:.3f}"
        if met
        else r.get("promote_reason")
    )
    deps.notifier.send(
        f"[ML 학습] quant {r.get('version') or '-'}: {r.get('status')} {acc} "
        f"승격={r.get('promoted')}"
    )
    return r


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
        return lambda: _reflect_body(deps)
    if job.kind == "ml_train":
        return lambda: _ml_train_body(deps)
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
                partial(
                    run_job,
                    job.id,
                    job_body(deps, job),
                    conn_factory=deps.conn_factory,
                    interval=job.interval_s is not None,
                ),
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
        cron = dict(job.cron)
        tz = ZoneInfo(cron.pop("timezone", "Asia/Seoul"))
        return CronTrigger(timezone=tz, **cron)
    return IntervalTrigger(seconds=int(job.interval_s or 60), timezone=KST)
