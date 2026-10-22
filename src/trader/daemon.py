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
from datetime import UTC, date, datetime, time, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trader import heartbeat as heartbeat_mod
from trader import names
from trader.config import KST, MARKET_TZ, AccountConfig, ScheduleConfig, Settings
from trader.control import control
from trader.heartbeat import Heartbeat
from trader.ledger import db
from trader.notify.telegram import Notifier, NullNotifier
from trader.orchestrator import run_store
from trader.platform import paths
from trader.platform.procs import pid_alive

logger = logging.getLogger("trader.daemon")
TELEGRAM_POLL_S = 30
FILLS_SYNC_TIMES = ((16, 10), (7, 10))  # KR 마감 후, US 마감 후 (KST)


# --- 잠금 -----------------------------------------------------------------------


class Lock:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _holder_pid(self) -> int:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return int(data.get("pid", 0))
        except (ValueError, OSError):
            return 0

    FRESH_S = 10  # 내용 없는 잠금이 이 시간 안에 생겼으면 다른 데몬이 쓰는 중

    def _fresh(self) -> bool:
        try:
            return (datetime.now(UTC).timestamp() - self.path.stat().st_mtime) < self.FRESH_S
        except OSError:
            return False

    def _create(self, tmp: Path, payload: str) -> None:
        """내용이 채워진 임시 파일을 잠금 이름에 원자적으로 붙인다 (os.link 는 이미 있으면 실패).

        생성→쓰기 두 단계면 그 사이에 뜬 두 번째 데몬이 빈 파일(pid 0)을 죽은 잠금으로 넘겨받는다.
        하드링크를 지원하지 않는 파일시스템이면 O_EXCL 생성 뒤 쓰기로 대신한다."""
        try:
            os.link(str(tmp), str(self.path))
        except FileExistsError:
            raise
        except OSError:
            fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)

    def acquire(self) -> bool:
        """원자적으로 만든다. 이미 있으면 pid 생존을 보고 죽은 데몬의 잠금만 넘겨받는다(재시도 1회).
        pid 를 읽을 수 없는 잠금은 방금 생긴 것이면 살아 있는 것으로(다른 데몬이 쓰는 중),
        오래됐으면 죽은 것으로 본다."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"pid": os.getpid(), "started_at": datetime.now(UTC).isoformat()})
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(payload, encoding="utf-8")
        try:
            for _ in range(2):
                try:
                    self._create(tmp, payload)
                except FileExistsError:
                    pid = self._holder_pid()
                    if pid and pid_alive(pid):  # 같은 프로세스가 두 번 잡는 것도 거부한다
                        logger.error("데몬이 이미 실행 중입니다 (pid %s): %s", pid, self.path)
                        return False
                    if not pid and self._fresh():
                        logger.error("다른 데몬이 잠금을 만드는 중입니다: %s", self.path)
                        return False
                    logger.warning("죽은 데몬의 잠금을 넘겨받습니다 (pid %s)", pid)
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                return True
            logger.error("잠금 경합이 반복돼 데몬을 띄우지 않습니다: %s", self.path)
            return False
        finally:
            tmp.unlink(missing_ok=True)

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


def market_date(market: str, now: datetime | date) -> date:
    """시장 현지 날짜 (주말 판정용). date 를 주면 그대로 쓴다."""
    if isinstance(now, datetime):
        return now.astimezone(MARKET_TZ.get(market, KST)).date()
    return now


def _easter(year: int) -> date:
    """부활절(그레고리력, Anonymous 알고리즘)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m_ = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m_) // 451
    month = (h + m_ - 7 * n + 114) // 31
    day = (h + m_ - 7 * n + 114) % 31 + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date, *, saturday_to_friday: bool = True) -> date | None:
    """토요일 → 금요일(신정은 예외: NYSE 는 전년 12/31 을 쉬지 않음), 일요일 → 월요일."""
    if d.weekday() == 5:
        return d - timedelta(days=1) if saturday_to_friday else None
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def us_holidays(year: int) -> frozenset[date]:
    """NYSE 정규 휴장일 규칙 계산(국가 애도일 등 임시 휴장은 제외)."""
    fixed = [
        (_observed(date(year, 1, 1), saturday_to_friday=False)),  # 신정
        _nth_weekday(year, 1, 0, 3),  # 마틴 루서 킹
        _nth_weekday(year, 2, 0, 3),  # 대통령의 날
        _easter(year) - timedelta(days=2),  # 성금요일
        _last_weekday(year, 5, 0),  # 현충일
        _observed(date(year, 6, 19)),  # 준틴스
        _observed(date(year, 7, 4)),  # 독립기념일
        _nth_weekday(year, 9, 0, 1),  # 노동절
        _nth_weekday(year, 11, 3, 4),  # 추수감사절
        _observed(date(year, 12, 25)),  # 성탄절
    ]
    return frozenset(d for d in fixed if d is not None and d.year == year)


def is_us_holiday(d: date) -> bool:
    return d in us_holidays(d.year)


def should_skip(
    market: str, today: datetime | date, market_client: Any | None, conn: sqlite3.Connection | None
) -> str | None:
    """paused·주말(시장 현지 날짜)·KR 휴장일(KIS)·US 휴장일(NYSE 규칙 계산 us_holidays)."""
    if conn is not None and control.is_paused(conn):
        return "일시정지"
    local = market_date(market, today)
    if local.weekday() >= 5:
        return "주말"
    today = local
    if market == "US":
        return "휴장일" if is_us_holiday(today) else None
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
    kind: str  # scan_analyze | monitor | reflect | ml_train | telegram_poll | fills_sync
    market: str | None = None
    cron: dict | None = None
    interval_s: int | None = None


def build_jobs(schedule: ScheduleConfig, settings: Settings, *, markets: list[str]) -> list[Job]:
    jobs: list[Job] = []
    for m in markets:
        ms = schedule.kr if m == "KR" else schedule.us
        lo = m.lower()
        # scan → analyze 는 한 잡으로 이어서 돈다: cron 두 개면 screener 세션이 늦을 때 analyze 가
        # _scan.json 없이 떠서 하루 분석이 통째로 빠지고, analyze 가 scan 을 인라인으로 돌리면 아직
        # 도는 scan 과 세션이 겹친다. 시각은 schedule 의 scan (analyze 시각은 더 쓰지 않는다).
        h, mi = ms.scan_hm()
        jobs.append(
            Job(
                f"{lo}_scan_analyze",
                "scan_analyze",
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
    heartbeat: Heartbeat | None = None,
) -> Any:
    """잡 본체를 감싼다. cron 잡은 runs 에 시작·종료를 남기고, interval 잡(monitor·텔레그램)은
    오류일 때만 남긴다(분 단위 행 폭주 방지). 예외는 로그·error 행으로 삼킨다.
    하트비트(있으면)는 interval 잡 포함 모든 잡의 시작·종료·상태를 파일에 남긴다."""
    run_id: int | None = None
    if heartbeat is not None:
        heartbeat.job_started(name, datetime.now(UTC))
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
        if heartbeat is not None:
            heartbeat.job_finished(name, datetime.now(UTC), "error", f"{type(e).__name__}: {e}")
        return None
    err = result.get("error") if isinstance(result, dict) else None
    if heartbeat is not None:
        skipped = isinstance(result, dict) and "skipped" in result
        heartbeat.job_finished(
            name,
            datetime.now(UTC),
            "error" if err else ("skipped" if skipped else "ok"),
            str(err) if err else None,
        )
    if run_id is None and err:  # interval 잡은 오류일 때만 행을 남긴다
        conn = conn_factory()
        try:
            run_id = db.start_run(conn, name, None)
        finally:
            conn.close()
    if run_id is not None:
        conn = conn_factory()
        try:
            db.finish_run(conn, run_id, "error" if err else "ok", str(err) if err else None)
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
    snapshot_fn: Callable[[list[str]], int] | None = (
        None  # reflect 전 피처 스냅샷(시장 목록 → 행 수)
    )
    stop_event: threading.Event = field(default_factory=threading.Event)  # SIGTERM 뒤 세션 중단


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
    return fn()


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
    from trader.orchestrator.run_store import run_date_for, write_checkpoint
    from trader.scan import quant, screener

    universe = deps.universe or load_universe()
    today = run_date_for(market, deps.now())
    from trader.ml import ranker

    ml = ranker.current(deps.state)
    conn = deps.conn_factory()
    try:
        runtime_watch = control.get_watchlist(conn, market=market)
        cands = quant.candidates(
            universe,
            market,
            state=deps.state,
            rank_fn=ml[0] if ml else None,
            watch_extra=runtime_watch,
            asof=today,
        )
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
            watch=[*universe.watch, *runtime_watch],
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
    write_checkpoint(run_store.market_dir(today, market, deps.state, create=True), "_scan", out)
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


def _analyze_body(deps: DaemonDeps, market: str, *, scan_error: str | None = None) -> dict:
    from trader.orchestrator.run_store import read_checkpoint, run_date_for
    from trader.orchestrator.session import SessionSpec, run_session
    from trader.sources.inputs import collect_and_save

    today = run_date_for(market, deps.now())
    scan = read_checkpoint(run_store.market_dir(today, market, deps.state), "_scan")
    if scan is None:
        # scan 이 실패했거나 결과가 없다. 조용히 ok 로 끝나면 그 시장의 하루 분석이 통째로 빠진
        # 것을 로그 말고는 알 수 없다 → 원인을 담은 알림 1건 + error 행
        cause = f"scan 실패: {scan_error}" if scan_error else "_scan.json 없음 (scan 지연·실패)"
        msg = f"[분석 생략] {market} {today}: {cause}, 보유 재분석도 생략"
        logger.error(msg)
        deps.notifier.send(msg)
        return {"targets": 0, "error": cause}
    targets = scan.get("analyze_targets") or []
    if not targets:
        logger.info("%s 분석 대상 없음 (빈 picks·보유·관심종목)", market)
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
    stopped = False
    for t in targets:
        if deps.stop_event.is_set():
            # SIGTERM 뒤 shutdown(wait=True) 가 이 잡을 기다린다. 남은 종목 세션을 계속 띄우면
            # launchd ExitTimeOut(세션 1회 기준) 안에 못 끝나 SIGKILL 된다
            left = len(targets) - ok - len(failed)
            logger.warning("종료 중: 남은 analyze 세션 %d건 생략 %s", left, market)
            stopped = True
            break
        conn = deps.conn_factory()
        shown = names.display(conn, _cache_dir(deps), market, t["ticker"])
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
            deps.notifier.send(f"[analyze 실패] {market} {shown}: {type(e).__name__}: {e}")
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
            deps.notifier.send(f"[analyze 실패] {market} {shown}: {res.error}")
    return {
        "targets": len(targets),
        "ok": ok,
        "failed": failed,
        "sessions": sessions,
        "stopped": stopped,
    }


def _monitor_body(deps: DaemonDeps, market: str) -> dict:
    from trader.orchestrator import monitor as monitor_mod
    from trader.orchestrator.run_store import run_date_for
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
        return monitor_mod.monitor_once(ctx, market=market, today=run_date_for(market, deps.now()))
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
                # US 체결은 ET 날짜로 조회해야 한다 (07:10 KST 는 전날 ET) → 시장별 현지 날짜
                out[a.id] = sync_fills(conn, b, a, deps.now())
    finally:
        conn.close()
    return out


def _reflect_body(deps: DaemonDeps) -> dict:
    """장 마감 후: 만기 결정 결과 채우기 → 반성 대기가 있는 시장만 세션 → 메모리 정리."""
    from trader.memory import outcomes, store
    from trader.orchestrator.reflect import pending, run_reflect

    features_n: int | None = None
    if deps.snapshot_fn is not None:
        try:
            features_n = int(deps.snapshot_fn(list(deps.settings.markets)))
        except Exception as e:  # noqa: BLE001 - 스냅샷 실패는 통보하고 반성은 계속
            logger.warning("피처 스냅샷 실패: %s", e)
            deps.notifier.send(f"[피처 스냅샷 실패] {e} — 만기 결과가 채워지지 않을 수 있음")
    conn = deps.conn_factory()
    try:
        matured = outcomes.mature(conn, state=deps.state, today=_today(deps))
        sessions = 0
        errors: list[str] = []
        for market in deps.settings.markets:
            if not pending(conn, market=market):
                continue
            try:
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
            except Exception as e:  # noqa: BLE001 - 한 시장의 실패가 다른 시장을 막지 않는다
                logger.exception("reflect 실패 %s", market)
                errors.append(f"{market}: {type(e).__name__}: {e}")
                deps.notifier.send(f"[reflect 실패] {market}: {e}")
                continue
            if res.session_id or res.fake:
                sessions += 1
            if not res.ok:
                errors.append(f"{market}: {res.error}")
                deps.notifier.send(f"[reflect 실패] {market}: {res.error}")
        pr = store.promote_and_prune(conn, now=deps.now(), settings=deps.settings.memory)
    finally:
        conn.close()
    return {
        "features": features_n,
        "matured": len(matured),
        "sessions": sessions,
        "errors": errors,
        "pruned": pr["deleted"],
    }


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
    rl = _rl_train(deps)
    if rl is not None:
        r["rl_sizer"] = rl
    return r


def _rl_train(deps: DaemonDeps) -> dict | None:
    """ml.yaml rl_sizer.enabled 일 때만 사이저도 학습한다 (승격은 수동). 실패는 통보만."""
    from trader.config import load_ml
    from trader.ml.rl import train as rl_train_mod

    try:
        cfg = load_ml()
        if not cfg.models.rl_sizer.enabled:
            return None
        conn = deps.conn_factory()
        try:
            r = rl_train_mod.train_sizer(cfg, conn, state=deps.state, now=deps.now)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 - 실험 학습 실패가 데몬을 막지 않는다
        logger.exception("RL 사이저 학습 실패")
        deps.notifier.send(f"[RL 학습 실패] {type(e).__name__}: {e}")
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    s = (r.get("summary") or {}).get("rl") or {}
    deps.notifier.send(
        f"[RL 학습] rl_sizer {r.get('version') or '-'}: {r.get('status')} "
        f"RL 수익 평균 {s.get('return_mean', '-')} (승격은 `trader rl promote` 수동)"
    )
    return r


def _scan_analyze_body(deps: DaemonDeps, market: str) -> dict:
    """scan 뒤 바로 analyze. scan 이 예외로 죽어도 통보하고 analyze 를 시도한다(_analyze_body 가
    `_scan.json` 없음을 알림·error 로 남긴다). 세션을 세기 전에 stale running 을 정리한다."""
    _close_stale_sessions(deps)
    scan_error: str | None = None
    try:
        _scan_body(deps, market)
    except Exception as e:  # noqa: BLE001 - scan 실패가 analyze 시도를 막지 않는다
        logger.exception("scan 실패 %s", market)
        scan_error = f"{type(e).__name__}: {e}"  # 알림은 _analyze_body 가 원인과 함께 1건만
    return _analyze_body(deps, market, scan_error=scan_error)


def _cache_dir(deps: DaemonDeps) -> Path:
    return (deps.state or paths.state_dir()) / "cache"


def job_body(deps: DaemonDeps, job: Job) -> Callable[[], Any]:
    if job.kind == "scan_analyze":
        return lambda: market_job(
            deps,
            job.market or "KR",
            "scan_analyze",
            lambda: _scan_analyze_body(deps, job.market or "KR"),
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


STALE_SESSION_AGE = timedelta(hours=1)


def _close_stale_sessions(deps: DaemonDeps) -> int:
    """1시간 넘게 running 인 agent_calls 를 error 로 닫는다. 데몬이 SIGKILL 로 죽거나 세션이
    중간에 죽으면 running 이 남아 상태 표시·오류율을 흐린다 → 기동 때와 매일 scan·analyze 앞에서."""
    from trader.ledger import repo

    conn = deps.conn_factory()
    try:
        n = repo.close_stale_running(
            conn, older_than_iso=(deps.now() - STALE_SESSION_AGE).isoformat()
        )
    finally:
        conn.close()
    if n:
        logger.warning("stale running 세션 %d건을 error 로 정리", n)
    return n


def run(
    deps: DaemonDeps,
    *,
    web: bool = True,
    scheduler_factory: Callable[[], Any] | None = None,
    lock_path: Path | None = None,
    install_signals: bool = True,
) -> dict:
    """잠금 → (web 스레드) → 잡 등록 → scheduler.start(). 반환은 start 가 끝난 뒤(종료 시).

    install_signals=False 는 트레이 모드(데몬이 비메인 스레드): 시그널은 호출자가 메인 스레드에서
    건다."""
    base = deps.state if deps.state is not None else paths.state_dir()
    lock = Lock(lock_path or base / "daemon.lock")
    if not lock.acquire():
        return {"ok": False, "error": "잠금 실패: 데몬이 이미 실행 중"}
    try:
        _close_stale_sessions(deps)
        if scheduler_factory is None:
            from apscheduler.schedulers.blocking import BlockingScheduler

            scheduler = BlockingScheduler(timezone=KST)
        else:
            scheduler = scheduler_factory()
        if web and deps.web_app_factory is not None:
            _start_web(deps.web_app_factory(), deps.settings.web.bind, deps.settings.web.port)
        jobs = build_jobs(deps.schedule, deps.settings, markets=list(deps.settings.markets))
        hb = Heartbeat(base / heartbeat_mod.FILE_NAME)
        hb.start(pid=os.getpid(), started_at=datetime.now(UTC), jobs=jobs)
        _install_heartbeat_listener(scheduler, hb)
        for job in jobs:
            scheduler.add_job(
                partial(
                    run_job,
                    job.id,
                    job_body(deps, job),
                    conn_factory=deps.conn_factory,
                    interval=job.interval_s is not None,
                    heartbeat=hb,
                ),
                _trigger(job),
                id=job.id,
                name=job.id,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=120,
            )
        logger.info("데몬 시작: 잡 %d개, web=%s", len(jobs), web)
        if install_signals:
            _install_signals(scheduler, deps.stop_event)
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("데몬 종료")
        hb.stop(datetime.now(UTC))
        return {"ok": True, "jobs": len(jobs)}
    finally:
        lock.release()


def _install_heartbeat_listener(scheduler: Any, hb: Heartbeat) -> None:
    """APScheduler 이벤트로 잡별 '다음 예정'·놓침을 하트비트에 적는다 (리스너가 없는 가짜는 통과).

    run_job 이 예외를 삼키므로 JOB_ERROR 는 사실상 오지 않고 결과는 run_job 이 적는다. 여기서는
    EXECUTED 뒤 next_run_time 갱신, MISSED·MAX_INSTANCES(앞 실행이 길어 건너뜀) 를 missed 로."""
    add = getattr(scheduler, "add_listener", None)
    if add is None:
        return
    try:
        from apscheduler import events as ev
    except ImportError:  # pragma: no cover - 의존성 누락
        return

    def _next(job_id: str) -> None:
        try:
            job = scheduler.get_job(job_id)
            hb.set_next(job_id, getattr(job, "next_run_time", None), at=datetime.now(UTC))
        except Exception as e:  # noqa: BLE001 - 하트비트는 보조 정보
            logger.debug("next_run_time 조회 실패 %s: %s", job_id, e)

    def on_started(_event: Any) -> None:
        for job in scheduler.get_jobs():
            _next(job.id)

    def on_job(event: Any) -> None:
        if event.code in (ev.EVENT_JOB_MISSED, ev.EVENT_JOB_MAX_INSTANCES):
            hb.job_missed(event.job_id, datetime.now(UTC))
        _next(event.job_id)

    add(on_started, ev.EVENT_SCHEDULER_STARTED)
    add(
        on_job,
        ev.EVENT_JOB_EXECUTED
        | ev.EVENT_JOB_ERROR
        | ev.EVENT_JOB_MISSED
        | ev.EVENT_JOB_MAX_INSTANCES,
    )


def make_signal_handler(
    scheduler: Any, stop_event: threading.Event | None = None
) -> Callable[[int, Any], None]:
    """SIGINT/SIGTERM → 실행 중인 잡이 끝나길 기다려 스케줄러를 멈춘 뒤 SystemExit.

    wait=True 라야 analyze 세션(최대 llm.timeout_s)이 끝난 뒤 finally 의 잠금 해제가 돈다.
    wait=False 면 잠금이 먼저 풀려 launchd KeepAlive 가 새 데몬을 띄우고 monitor·텔레그램 폴링이
    두 프로세스에서 돌 수 있다."""

    state = {"shutting_down": False}

    def handler(signum: int, frame: Any) -> None:
        if state["shutting_down"]:
            # shutdown(wait=True) 대기 중 두 번째 시그널: 여기서 SystemExit 을 올리면 대기가 끊겨
            # 잡 스레드(주문 중일 수 있음)보다 잠금이 먼저 풀리고 launchd 가 새 데몬을 띄운다
            logger.warning("시그널 %s 재수신, 이미 종료 중입니다 (실행 중인 잡을 기다림)", signum)
            return
        state["shutting_down"] = True
        if stop_event is not None:
            stop_event.set()  # analyze 가 남은 종목 세션을 띄우지 않도록
        logger.info("시그널 %s 수신, 실행 중인 잡을 마치고 데몬을 종료합니다", signum)
        try:
            scheduler.shutdown(wait=True)
        except Exception:  # noqa: BLE001 - 종료 중 오류는 무시
            pass
        raise SystemExit(0)

    return handler


def _install_signals(scheduler: Any, stop_event: threading.Event | None = None) -> None:
    import signal

    handler = make_signal_handler(scheduler, stop_event)
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
