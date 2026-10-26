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
from trader import jobs as jobq
from trader import names
from trader.config import KST, MARKET_TZ, AccountConfig, ScheduleConfig, Secrets, Settings
from trader.control import control
from trader.heartbeat import Heartbeat
from trader.ledger import db
from trader.notify import messages
from trader.notify.telegram import Notifier, NullNotifier
from trader.orchestrator import run_store
from trader.platform import bundle, paths, procs
from trader.platform.procs import process_start, same_process

logger = logging.getLogger("trader.daemon")
SCALP_INTERVAL_S = 60  # 분 단위 단타 틱 (Phase 23 S)
OPS_CHECK_S = 1800  # 운영 점검 주기 (Phase 23 A)
BALANCE_REFRESH_S = 300  # 정규장 잔고 주기 갱신(웹 실시간 — 조회만)
TELEGRAM_POLL_S = 30
QUEUE_JOB_ID = "job_queue"  # 작업 대기열 소비 잡 (jobs.py)
FAST_QUEUE_JOB_ID = "job_queue_fast"  # 빠른 레인: 재평가·가벼운 잡(heavy 락 밖, 전체 검토 P2)
QUEUE_INTERVAL_S = 10
QUEUED_MAX_AGE = timedelta(hours=6)  # 데몬이 꺼진 동안 쌓인 요청은 만료
BOOTSTRAP_RETRY_AGE = timedelta(hours=24)  # 피처 스토어 부트스트랩 백필 실패 뒤 재시도 간격
FILLS_SYNC_TIMES = ((16, 10), (7, 10))  # KR 마감 후, US 마감 후 (KST)


# --- 잠금 -----------------------------------------------------------------------


class Lock:
    """데몬 단일 인스턴스 잠금. `<path>.os` 에 **수명 동안 쥐는 OS 파일 잠금**을 먼저 잡고(전체 검토
    P4 — 죽은 잠금 인수를 두 프로세스가 동시에 판단해 둘 다 뜨던 경합을 OS 가 직렬화), 표시·생존
    판정용 pid 파일(`path`)은 예전처럼 쓴다(heartbeat.read_status 가 읽는다)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._hold = None

    def _holder(self) -> tuple[int, str | None]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return int(data.get("pid", 0)), data.get("proc_start") or None
        except (ValueError, OSError):
            return 0, None

    def _holder_pid(self) -> int:
        return self._holder()[0]

    def held(self) -> bool:
        """지금 살아 있는 (이 잠금을 잡은 그) 프로세스가 쥐고 있는지 — 잡지 않고 보기만 한다."""
        pid, start = self._holder()
        return bool(pid) and same_process(pid, start)

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
        from trader.platform import filelock

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._hold = filelock.try_hold(self.path.with_name(self.path.name + ".os"))
        if self._hold is None:
            logger.error("데몬이 이미 실행 중입니다 (OS 잠금): %s", self.path)
            return False
        if not self._acquire_pidfile():
            filelock.release_hold(self._hold)
            self._hold = None
            return False
        return True

    def _acquire_pidfile(self) -> bool:
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "started_at": datetime.now(UTC).isoformat(),
                # pid 재사용 대비(재부팅 뒤 다른 프로세스가 같은 pid 를 받으면 '실행 중' 으로 오판해
                # 데몬이 영영 뜨지 않았다): 프로세스 시작 시각 식별자를 함께 적고 비교한다
                "proc_start": process_start(os.getpid()),
            }
        )
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(payload, encoding="utf-8")
        try:
            for _ in range(2):
                try:
                    self._create(tmp, payload)
                except FileExistsError:
                    pid, start = self._holder()
                    if pid and same_process(pid, start):  # 같은 프로세스가 두 번 잡는 것도 거부
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
        from trader.platform import filelock

        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if int(data.get("pid", 0)) == os.getpid():
                    self.path.unlink()
        except (ValueError, OSError):
            pass
        finally:
            filelock.release_hold(self._hold)
            self._hold = None


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


def us_early_closes(year: int) -> frozenset[date]:
    """NYSE 조기 마감(13:00 ET) 규칙: 추수감사절 다음 날, 7/3·12/24 가 월~목일 때
    (금요일이면 다음 날 토요일 휴일의 대체 휴장이라 전일 휴장, 주말이면 장이 없다)."""
    out = {_nth_weekday(year, 11, 3, 4) + timedelta(days=1)}
    for d in (date(year, 7, 3), date(year, 12, 24)):
        if d.weekday() <= 3:
            out.add(d)
    return frozenset(out)


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
    kind: str  # scan_analyze|monitor|reflect|ml_train|telegram_poll|fills_sync|job_queue|reconcile
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
        # 장 마감 일일 리포트(Stock D-095): 시장 현지 16:20, 휴장일은 market_job 이 건너뛴다
        jobs.append(
            Job(
                f"{lo}_daily_report",
                "daily_report",
                m,
                cron={
                    "hour": 16,
                    "minute": 20,
                    "day_of_week": "mon-fri",
                    "timezone": str(MARKET_TZ[m]),  # schedule 의 US tz 는 KST 라 현지 마감 기준으로
                },
            )
        )
        # 분 단위 단타(Phase 23 S): monitor 와 별도 60초 잡, 장중 창·모의 전용·기본 끔(body 가 판단)
        jobs.append(Job(f"{lo}_scalp", "scalp", m, interval_s=SCALP_INTERVAL_S))
        # 테마 열기 감지(Phase 25): 장중 창만, 가벼운 잡(KIS 순위 3회 + 원장 읽기)
        if settings.theme.enabled:
            jobs.append(Job(f"{lo}_theme", "theme", m, interval_s=settings.theme.interval_s))
        # 야간 KIS 대조(Phase 21): 체결 동기화 뒤, 시장 현지 시각. 가벼운 잡(heavy 락 밖)
        rt = _hm(getattr(schedule.reconcile, lo))
        jobs.append(
            Job(
                f"{lo}_reconcile",
                "reconcile",
                m,
                cron={
                    "hour": rt.hour,
                    "minute": rt.minute,
                    "day_of_week": "mon-fri",
                    "timezone": ms.tz,
                },
            )
        )
    if settings.rebalance.enabled:  # 주간 리밸런스(Phase 25): 시장 현지 시각, 운영일 1회
        for m in markets:
            ms = schedule.kr if m == "KR" else schedule.us
            rt = _hm(getattr(schedule.rebalance, m.lower()))
            jobs.append(
                Job(
                    f"{m.lower()}_rebalance",
                    "rebalance",
                    m,
                    cron={
                        "hour": rt.hour,
                        "minute": rt.minute,
                        "day_of_week": schedule.rebalance.weekday,
                        "timezone": ms.tz,
                    },
                )
            )
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
    jobs.append(Job(QUEUE_JOB_ID, "job_queue", interval_s=QUEUE_INTERVAL_S))
    jobs.append(Job(FAST_QUEUE_JOB_ID, "job_queue_fast", interval_s=QUEUE_INTERVAL_S))
    jobs.append(Job("news_collect", "news_collect", interval_s=schedule.news_collect.interval_s))
    jobs.append(Job("ops_check", "ops_check", interval_s=OPS_CHECK_S))  # 운영 점검(Phase 23 A)
    jobs.append(Job("balance_refresh", "balance_refresh", interval_s=BALANCE_REFRESH_S))
    jobs.append(Job("saveticker", "saveticker", interval_s=SAVETICKER_WATCH_S))
    jobs.append(Job("ledger_backup", "ledger_backup", cron={"hour": 17, "minute": 10}))
    if "KR" in markets:  # 권리 일정 수집(블랙아웃이 꺼져 있어도 모아 둔다 — 켤 때 바로 쓰게)
        jobs.append(
            Job(
                "ksd_events",
                "ksd_events",
                "KR",
                cron={"hour": 7, "minute": 5, "day_of_week": "mon-fri"},
            )
        )
    if settings.briefing.enabled and "KR" in markets:
        bt = _hm(settings.briefing.time)
        jobs.append(
            Job(
                "morning_briefing",
                "briefing",
                "KR",
                cron={"hour": bt.hour, "minute": bt.minute, "day_of_week": "mon-fri"},
            )
        )
    jobs.append(
        Job("paper_review", "paper_review", cron={"hour": 9, "minute": 0, "day_of_week": "sat"})
    )
    if settings.llm.usage_probe.enabled:
        jobs.append(
            Job("usage_probe", "usage_probe", interval_s=settings.llm.usage_probe.interval_min * 60)
        )
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


class HeavyWanted:
    """heavy 락을 기다리는 cron 잡 수(전체 검토 P8). Event 하나로는 cron 둘이 기다릴 때 먼저 끝난
    쪽이 clear 해 남은 쪽의 양보 요청이 사라졌다 — 수를 세어 0 이 될 때만 꺼진다."""

    def __init__(self) -> None:
        self._n = 0
        self._lock = threading.Lock()

    def enter(self) -> None:
        with self._lock:
            self._n += 1

    def leave(self) -> None:
        with self._lock:
            self._n = max(0, self._n - 1)

    def is_set(self) -> bool:
        with self._lock:
            return self._n > 0


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
    # 피처 스토어 작업: (action: backfill|snapshot|derive, markets, *, progress, **kw) -> 행 수
    features_fn: Callable[..., int] | None = None
    # 무거운 잡(scan_analyze·reflect·ml_train·대기열 요청)은 한 번에 하나. cron 은 기다리고
    # 대기열 틱은 잠겨 있으면 claim 하지 않는다
    heavy_lock: threading.Lock = field(default_factory=threading.Lock)
    scalp_judge: Any | None = None  # Phase 25 단타 판정자(scalp.judge 일 때 _scalp_body 가 만든다)
    saveticker_proc: Any | None = None  # 이 데몬이 띄운 세이브티커 수집 서비스(Popen)
    saveticker_home: Path | None = None  # 그 서비스의 수집 폴더(종료 때 남은 작업 정리)
    saveticker_pruned: date | None = None  # 수집 폴더 보관 기간 정리를 한 날(운영일 1회)
    # cron 잡이 heavy 락을 기다리는 중이면 선다. 대기열의 긴 잡(백필)은 진행 콜백에서 이것을 보고
    # 락을 잠시 내놓는다(종목 경계) — 아침 scan 이 백필 뒤로 수 시간 밀리지 않도록
    heavy_wanted: HeavyWanted = field(default_factory=lambda: HeavyWanted())
    # 종목별 결정 실행 직렬화(PR #37 리뷰): 빠른 레인의 재평가가 같은 종목 analyze 세션과 겹치면
    # 둘 다 열린 주문 확인을 통과해 같은 잔고로 주문을 낼 수 있다 → (시장, 종목) 락
    ticker_locks: dict[tuple[str, str], threading.Lock] = field(default_factory=dict)
    ticker_locks_guard: threading.Lock = field(default_factory=threading.Lock)


HEAVY_POLL_S = 30.0  # heavy 락 대기 폴링 간격
HEAVY_WAIT_NOTIFY_S = 600.0  # cron 이 이만큼 기다리면 알림 1건


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

    if deps.stop_event.is_set():
        return {"skipped": "종료 중"}
    universe = deps.universe or load_universe()
    today = run_date_for(market, deps.now())
    from trader.ml import ranker

    ml = ranker.current(deps.state, market=market)
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
            cancel=deps.stop_event,
            universe=universe,
            discovery=True,
            sources_cfg=deps.sources_cfg,
            http=getattr(deps.input_deps, "http", None),
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
    try:
        deps.notifier.send(scan_line(out))
    except Exception:  # noqa: BLE001 - 알림 실패가 scan 결과를 바꾸지 않는다
        logger.debug("scan 알림 실패", exc_info=True)
    return out


def scan_line(scan: dict) -> str:
    """scan 결과 알림 한 줄: picks 와 뉴스 발굴 요약(STATUS·텔레그램 /status 와 같은 문구)."""
    from trader.scan import discovery as discovery_mod

    picks = [p.get("ticker") for p in scan.get("picks") or []]
    n_t = len(scan.get("analyze_targets") or [])
    cap = " · 세션 상한" if scan.get("cap_hit") else ""
    return messages.event(
        "스캔",
        f"{scan.get('market')} {scan.get('date')}: 선정 {len(picks)}"
        f"({', '.join(str(p) for p in picks) or '-'}) · 분석 대상 {n_t} · "
        f"{discovery_mod.line(scan.get('discovery'))}{cap}",
    )


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
    """`_scan.json` 의 분석 대상으로 `_analyze_targets` 를 돌린다."""
    from trader.orchestrator.run_store import read_checkpoint, run_date_for

    today = run_date_for(market, deps.now())
    scan = read_checkpoint(run_store.market_dir(today, market, deps.state), "_scan")
    if scan is None:
        # scan 이 실패했거나 결과가 없다. 조용히 ok 로 끝나면 그 시장의 하루 분석이 통째로 빠진
        # 것을 로그 말고는 알 수 없다 → 원인을 담은 알림 1건 + error 행
        cause = f"scan 실패: {scan_error}" if scan_error else "_scan.json 없음 (scan 지연·실패)"
        msg = messages.event("분석 생략", f"{market} {today}", f"{cause}, 보유 재분석도 생략")
        logger.error(msg)
        deps.notifier.send(msg)
        return {"targets": 0, "error": cause}
    targets = scan.get("analyze_targets") or []
    if not targets:
        logger.info("%s 분석 대상 없음 (빈 picks·보유·관심종목)", market)
        return {"targets": 0}
    targets, skipped = _skip_held_by_profile(deps, market, targets, today)
    out = _analyze_targets(deps, market, targets, today=today) if targets else {"targets": 0}
    if skipped:
        out["skipped_held"] = skipped
    return out


def _skip_held_by_profile(
    deps: DaemonDeps, market: str, targets: list[dict], today: date
) -> tuple[list[dict], list[dict]]:
    """보유 종목(`source == "held"`, picks·watch 에 없는 것)은 최근 결정의 보유기간 프로필
    `daily_reanalyze_every` 운영일마다만 아침 재분석한다(Phase 23 T). 그 사이 날은 monitor 손절·
    트리거·뉴스 재평가가 본다. 결정이 없는 보유분은 항상 분석."""
    from trader.ledger import repo
    from trader.risk import horizon

    keep, skipped = [], []
    conn = deps.conn_factory()
    try:
        for t in targets:
            if t.get("source") != "held":
                keep.append(t)
                continue
            latest = repo.latest_decision(conn, market, t["ticker"])
            if latest is None:
                keep.append(t)
                continue
            name, prof = horizon.of_decision(deps.settings.risk, latest.get("data"))
            last_d = date.fromisoformat(str(latest["date"]))
            if horizon.reanalyze_due(prof, last_d, today):
                keep.append(t)
            else:
                skipped.append(
                    {
                        "ticker": t["ticker"],
                        "profile": name,
                        "last": last_d.isoformat(),
                        "every": prof.daily_reanalyze_every,
                    }
                )
    finally:
        conn.close()
    if skipped:
        logger.info("%s 보유 재분석 건너뜀(프로필 주기): %s", market, skipped)
    return keep, skipped


def _snapshot_listed(deps: DaemonDeps, market: str, t: dict) -> None:
    """유니버스 밖 발굴 종목은 피처 스토어에 행이 없어 05_quant·섹터 비교가 빈다 → 분석 직전 그
    종목만 스냅샷 1회(`features.snapshot(tickers=)`). 시세 클라이언트가 없으면 건너뛰고 실패는
    경고만."""
    if deps.features_fn is None:
        return
    try:
        n = deps.features_fn(
            "snapshot",
            [market],
            progress=lambda s: None,
            tickers=[(t["ticker"], t.get("exchange"))],
        )
        logger.info("발굴 종목 피처 스냅샷 %s %s: %s행", market, t["ticker"], n)
    except Exception as e:  # noqa: BLE001 - 피처 없이도 세션은 돈다
        logger.warning("발굴 종목 피처 스냅샷 실패 %s %s: %s", market, t["ticker"], e)


def _analyze_targets(
    deps: DaemonDeps,
    market: str,
    targets: list[dict],
    *,
    today: date,
    dry_run: bool | None = None,
) -> dict:
    """대상 종목마다 수집 → 05_quant → 세션 1회. dry_run 이 None 이면 데몬 설정을 따른다
    (대기열의 단일 종목 분석은 `deps.dry_run or 요청값` 으로 데몬 --dry-run 을 뒤집지 못한다)."""
    from trader.orchestrator.session import SessionSpec, run_session
    from trader.sources.inputs import collect_and_save

    dry = deps.dry_run if dry_run is None else (deps.dry_run or dry_run)

    def collect(t: dict) -> None:
        # 종목 **직전**에 수집한다: 전 종목을 먼저 모으면 마지막 종목 입력이 세션 때 수십 분 늙는다
        if deps.input_deps is None or deps.sources_cfg is None:
            return
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
        if t.get("discovery") == "listed":
            _snapshot_listed(deps, market, t)
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
        collect(t)
        conn = deps.conn_factory()
        shown = names.display(conn, _cache_dir(deps), market, t["ticker"])
        try:
            with _ticker_lock(deps, market, t["ticker"]):
                res = runner(
                    SessionSpec(
                        market=market, date=today, tickers=[t], dry_run=dry, asof=deps.now()
                    ),
                    deps.settings,
                    conn,
                    deps.accounts,
                    state=deps.state,
                    now=deps.now,
                    brokers=deps.brokers,
                    notifier=deps.notifier,
                    cancel=deps.stop_event,
                )
        except Exception as e:  # noqa: BLE001 - 한 종목 세션 실패가 나머지를 막지 않는다
            logger.exception("analyze 세션 기동 실패 %s %s", market, t["ticker"])
            failed.append(t["ticker"])
            deps.notifier.send(messages.failure("분석 실패", f"{market} {shown}", e))
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
            deps.notifier.send(messages.event("분석 실패", f"{market} {shown}: {res.error}"))
    return {
        "targets": len(targets),
        "ok": ok,
        "failed": failed,
        "sessions": sessions,
        "stopped": stopped,
    }


def _proxy_candles(deps: DaemonDeps, market: str, days: int = 260) -> list:
    from trader.broker.models import drop_incomplete_tail

    inst = deps.settings.hedge.index_proxy.get(market)  # type: ignore[arg-type]
    if inst is None or deps.market_client is None:
        return []
    if market == "US":
        got = deps.market_client.get_daily_us(inst.symbol, inst.exchange or "AMS", days=days)
    else:
        got = deps.market_client.get_daily_kr(inst.symbol, days=days)
    # 개장 전 거래량 0 스텁 봉이 들어가면 20일 수익률·변동성이 오늘의 가짜 종가로 계산된다
    return drop_incomplete_tail(got)


def _proxy_closes(deps: DaemonDeps, market: str, days: int = 260) -> list:
    return [c.close for c in _proxy_candles(deps, market, days)]


def _mature_market_views(deps: DaemonDeps, conn: sqlite3.Connection) -> int:
    """reflect 잡: 만기 지난 시장 제안에 지수 proxy 수익·적중을 채운다 (실패는 경고만)."""
    from trader.orchestrator import market_view as mv
    from trader.orchestrator.run_store import run_date_for

    n = 0
    for market in deps.settings.markets:
        try:
            candles = _proxy_candles(deps, market, days=120)
            n += mv.mature_views(
                conn,
                market,
                today=run_date_for(market, deps.now()),
                candles=[(c.date, c.close) for c in candles],
            )
        except Exception as e:  # noqa: BLE001 - 채점 실패가 복기를 막지 않는다
            logger.warning("시장 제안 채점 실패 %s: %s", market, e)
    return n


def _vix(deps: DaemonDeps, market: str) -> float | None:
    """US 국면의 변동성 지표는 FRED VIX. 키가 없거나 실패하면 None → 실현변동성으로 대체."""
    if market != "US" or deps.secrets is None or not getattr(deps.secrets, "FRED_API_KEY", None):
        return None
    try:
        import httpx

        from trader.sources.macro import Fred

        with httpx.Client() as http:
            return float(Fred(deps.secrets.FRED_API_KEY, http).latest("VIXCLS")["value"])
    except Exception as e:  # noqa: BLE001 - VIX 는 보조 지표
        logger.warning("VIX 조회 실패, 실현변동성으로 대체: %s", e)
        return None


def _flow_weak(deps: DaemonDeps, market: str) -> bool:
    """Phase 21 보조 지표: KR 지수 proxy 외인 순매수 20일·5일 합이 모두 음수. `hedge.flow_signal`
    이 꺼져 있거나 KR 이 아니거나 조회에 실패하면 False(지표 없음)."""
    from trader.risk import regime as regime_mod

    cfg = deps.settings.hedge
    if not getattr(cfg, "flow_signal", False) or market != "KR" or deps.market_client is None:
        return False
    inst = cfg.index_proxy.get(market)  # type: ignore[arg-type]
    if inst is None:
        return False
    try:
        flows = deps.market_client.get_investor_kr(inst.symbol, days=20)
    except Exception as e:  # noqa: BLE001 - 보조 지표
        logger.warning("외인 수급 조회 실패 %s: %s", market, e)
        return False
    return regime_mod.flow_weak(flows)


def _regime_body(deps: DaemonDeps, market: str) -> dict:
    """국면 판정 갱신 (scan_analyze 앞, 운영일 1회). 실패하면 직전 상태 유지 + 경고."""
    from trader.orchestrator.run_store import run_date_for
    from trader.risk import regime as regime_mod

    if not deps.settings.hedge.enabled:
        return {"skipped": "hedge.enabled=false"}
    try:
        closes = _proxy_closes(deps, market)
        if not closes:
            return {"error": "지수 proxy 일봉 없음"}
        conn = deps.conn_factory()
        try:
            out = regime_mod.update(
                conn,
                market,
                closes=closes,
                cfg=deps.settings.hedge,
                today=run_date_for(market, deps.now()),
                vix=_vix(deps, market),
                now=deps.now(),
                flow_weak=_flow_weak(deps, market),
            )
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 - 국면 갱신 실패가 스캔을 막지 않는다
        logger.exception("국면 판정 실패 %s", market)
        deps.notifier.send(
            messages.event(
                "국면 판정 실패", f"{market}: {type(e).__name__}: {e} — 직전 상태를 유지해요"
            )
        )
        return {"error": f"{type(e).__name__}: {e}"}
    if out.get("state") != out.get("prev_state"):
        deps.notifier.send(
            messages.regime_change(
                market=market,
                before=out.get("prev_state"),
                after=out["state"],
                count=regime_mod.count_text(out),
            )
        )
    return out


def _regime_apply_view(deps: DaemonDeps, market: str) -> dict:
    """시장 세션 뒤: 오늘 제안이 RISK_OFF(확신 ≥ hedge.agent_min_confidence)면 전략가 신호를 4번째
    지표로 켜고 국면을 다시 판정한다(같은 운영일 재판정, streak 겹침 없음). 전환이면 알림 1건.
    RISK_OFF 가 아니면 신호를 끈 채 재판정(어제 켜진 신호가 남지 않게)."""
    from trader.orchestrator import market_view as mv
    from trader.orchestrator.run_store import run_date_for
    from trader.risk import regime as regime_mod

    cfg = deps.settings.hedge
    if not cfg.enabled or not cfg.agent_signal:
        return {"skipped": "hedge.agent_signal=false"}
    today = run_date_for(market, deps.now())
    conn = deps.conn_factory()
    try:
        view = mv.load_view(conn, market, today)
        on = bool(
            view
            and view.get("stance") == "RISK_OFF"
            and float(view.get("confidence") or 0) >= cfg.agent_min_confidence
        )
        before = (regime_mod.load(conn, market) or {}).get("state")
        out = regime_mod.reapply(
            conn, market, agent_risk_off=on, cfg=cfg, today=today, now=deps.now()
        )
    finally:
        conn.close()
    if out is None:
        return {"skipped": "오늘 국면 판정 없음"}
    if out.get("state") != before:
        deps.notifier.send(
            messages.regime_change(
                market=market, before=before, after=out["state"], count=regime_mod.count_text(out)
            )
        )
    return {**out, "agent_risk_off": on}


def _market_view_body(deps: DaemonDeps, market: str) -> dict:
    """시장 세션(Phase 18): 국면 갱신 뒤 운영일 1회. 거시 애널리스트 → 토론 → 전략가 제안.
    실패·상한이면 제안 없이(규칙 기본값) 간다. 알림은 제안이 왔을 때만 1건."""
    from trader.orchestrator import market_view as mv
    from trader.orchestrator import tools
    from trader.orchestrator.run_store import run_date_for

    cfg = deps.settings.hedge
    if not cfg.enabled or not cfg.market_session:
        return {"skipped": "hedge.market_session=false"}
    if deps.stop_event.is_set():
        return {"skipped": "종료 중"}
    today = run_date_for(market, deps.now())
    conn = deps.conn_factory()
    try:
        if mv.load_view(conn, market, today) is not None:
            return {"skipped": "오늘 이미 제안 있음"}
        cap_hit = (
            tools.sessions_today(conn, deps.now()) >= deps.settings.llm.daily_session_cap
            or mv.market_sessions_today(conn, deps.now()) >= deps.settings.llm.market_session_cap
        )
        inputs = mv.build_inputs(
            conn,
            market,
            today,
            settings=deps.settings,
            asof=deps.now(),
            market_client=deps.market_client,
            secrets=deps.secrets,
            accounts=deps.accounts,
            brokers=deps.brokers,
            state=deps.state,
            now=deps.now,
        )
        out = mv.run_market_view(
            deps.settings,
            conn,
            deps.accounts,
            market=market,
            date=today,
            inputs=inputs,
            cap_hit=cap_hit,
            state=deps.state,
            now=deps.now,
            brokers=deps.brokers,
            session_runner=deps.session_runner,
            cancel=deps.stop_event,
        )
    finally:
        conn.close()
    view = out.get("view")
    if view:
        deps.notifier.send(
            messages.market_view(
                market=market,
                stance=view.get("stance"),
                hedge_weight=view.get("hedge_weight"),
                leveraged=view.get("leveraged", True),
                rationale=view.get("rationale"),
            )
        )
    elif out.get("error"):
        deps.notifier.send(
            messages.event("시장 세션 실패", f"{market}: {out['error']} — 규칙 기본값으로 진행해요")
        )
    return out


def _cash_park_body(deps: DaemonDeps) -> dict:
    """현금 단기금리 ETF(기본 끔, Stock D-115): monitor 틱에서 헤지 뒤, 운영일 1회·KR·일시정지
    보류."""
    from trader.orchestrator import cash_park
    from trader.orchestrator.executor import ExecContext, regime_state
    from trader.orchestrator.run_store import run_date_for

    if deps.market_client is None:
        return {"skipped": "시세 클라이언트 없음"}
    conn = deps.conn_factory()
    try:
        if control.is_paused(conn):
            return {"skipped": "일시정지"}
        ectx = ExecContext(
            settings=deps.settings,
            accounts=deps.accounts,
            conn=conn,
            brokers=deps.brokers,
            notifier=deps.notifier,
            state=deps.state,
            now=deps.now,
        )
        return cash_park.run(
            ectx,
            market_client=deps.market_client,
            today=run_date_for("KR", deps.now()),
            regime=regime_state(ectx, "KR"),
            dry_run=deps.dry_run,
        )
    finally:
        conn.close()


def _hedge_body(deps: DaemonDeps, market: str) -> dict:
    """monitor 첫 창 안 틱에서 운영일 1회: bear 면 인버스 진입, 벗어났으면 해제(paused 면 보류)."""
    from trader.orchestrator import hedge as hedge_mod
    from trader.orchestrator.executor import ExecContext
    from trader.orchestrator.run_store import run_date_for

    cfg = deps.settings.hedge
    if not cfg.enabled or deps.market_client is None:
        return {"skipped": "비활성"}
    today = run_date_for(market, deps.now())
    conn = deps.conn_factory()
    try:
        if control.get_flag(conn, hedge_mod.done_key(market, today)):
            return {"skipped": "오늘 이미 실행"}
        if control.is_paused(conn):
            flag = f"hedge_paused_notice:{market}:{today.isoformat()}"
            if not control.get_flag(conn, flag):
                control.set_flag(conn, flag, True)
                deps.notifier.send(
                    messages.event(
                        "헤지 보류: 일시정지", f"{market} — 재개하면 다음 점검에서 실행해요"
                    )
                )
            return {"skipped": "일시정지"}
        ectx = ExecContext(
            settings=deps.settings,
            accounts=deps.accounts,
            conn=conn,
            brokers=deps.brokers,
            notifier=deps.notifier,
            state=deps.state,
            now=deps.now,
        )
        mc = deps.market_client

        def quote(inst):
            q = (
                mc.get_price_us(inst.symbol, inst.exchange or "AMS")
                if market == "US"
                else mc.get_price_kr(inst.symbol)
            )
            return q.price

        return hedge_mod.rebalance(
            ectx, market=market, today=today, cfg=cfg, quote_fn=quote, dry_run=deps.dry_run
        )
    finally:
        conn.close()


def _reprice_body(deps: DaemonDeps, market: str) -> dict:
    """아침 미체결 재호가(Phase 23 B): monitor 틱에서 개장 + after_min 뒤 운영일 1회."""
    from trader.orchestrator import reprice as reprice_mod
    from trader.orchestrator.executor import ExecContext
    from trader.orchestrator.run_store import run_date_for

    cfg = deps.settings.risk.reprice
    if not cfg.enabled or deps.market_client is None or deps.dry_run:
        return {"skipped": "비활성"}
    ms = deps.schedule.kr if market == "KR" else deps.schedule.us
    local = deps.now().astimezone(ZoneInfo(ms.tz))
    oh, om = ms.monitor.start_hm()
    since_open = (local.hour * 60 + local.minute) - (oh * 60 + om)
    if since_open < cfg.after_min:
        return {"skipped": "개장 뒤 대기"}
    today = run_date_for(market, deps.now())
    conn = deps.conn_factory()
    try:
        if control.get_flag(conn, reprice_mod.done_key(market, today)):
            return {"skipped": "오늘 이미 실행"}
        ectx = ExecContext(
            settings=deps.settings,
            accounts=deps.accounts,
            conn=conn,
            brokers=deps.brokers,
            notifier=deps.notifier,
            state=deps.state,
            now=deps.now,
        )
        mc = deps.market_client

        def quote(ticker: str, exchange: str | None):
            if market == "US":
                if not exchange:
                    raise ValueError(f"US 거래소 코드 없음: {ticker}")
                return mc.get_price_us(ticker, exchange).price
            return mc.get_price_kr(ticker).price

        cache = _cache_dir(deps)
        return reprice_mod.reprice(
            ectx,
            market=market,
            today=today,
            cfg=cfg,
            quote_fn=quote,
            exchanges={u.symbol: u.exchange for u in (deps.universe.us if deps.universe else [])},
            dry_run=deps.dry_run,
            label=lambda t: names.display(conn, cache, market, t),
        )
    finally:
        conn.close()


def _enqueue_reevaluate(deps: DaemonDeps, params: dict) -> int | None:
    """monitor → 큐. 같은 종목 요청이 대기·실행 중이면 None(중복)."""
    conn = deps.conn_factory()
    try:
        return jobq.enqueue(conn, "reevaluate", params, source="daemon", now=deps.now())
    except jobq.DuplicateRequest:
        return None
    finally:
        conn.close()


def _recollect(deps: DaemonDeps, market: str, ticker: str, exchange: str | None) -> None:
    from trader.sources.inputs import collect_and_save

    if deps.input_deps is None or deps.sources_cfg is None:
        return
    try:
        collect_and_save(
            ticker=ticker,
            market=market,
            exchange=exchange,
            asof=deps.now(),
            cfg=deps.sources_cfg,
            secrets=deps.secrets,
            deps=deps.input_deps,
            state=deps.state,
            force=True,
        )
    except Exception as e:  # noqa: BLE001 - 재수집 실패면 기존 입력으로 재평가
        logger.warning("뉴스 재평가 재수집 실패 %s %s: %s", market, ticker, e)


def _reevaluate_request(deps: DaemonDeps, p: dict) -> dict:
    """큐에서 꺼낸 재평가 1건: 트리거 행을 읽어 `resume.reevaluate` 세션을 돈다(heavy)."""
    from trader.ledger import repo
    from trader.orchestrator import resume
    from trader.orchestrator.run_store import run_date_for
    from trader.orchestrator.session import run_session

    _close_stale_sessions(deps)
    market = p["market"]
    conn = deps.conn_factory()
    try:
        trig = repo.get_trigger(conn, int(p["trigger_id"]))
        if trig is None:
            return {"error": f"트리거 없음 #{p['trigger_id']}"}
        if trig.get("handled_at"):
            return {"skipped": "이미 처리된 트리거"}
        today = run_date_for(market, deps.now())
        if trig["kind"] == "news":
            # 뉴스 재평가(Phase 23): 트레이더가 새 뉴스를 보도록 00_inputs 를 다시 수집한다
            _recollect(deps, market, p["ticker"], p.get("exchange"))
        if (
            deps.stop_event.is_set()
        ):  # 재수집(최대 수 분) 사이 종료 요청이 왔으면 세션을 띄우지 않는다
            return {"skipped": "종료 중"}
        with _ticker_lock(deps, market, p["ticker"]):  # 같은 종목 analyze 와 겹치지 않게
            res = resume.reevaluate(
                deps.settings,
                conn,
                deps.accounts,
                market=market,
                date=today,
                ticker=p["ticker"],
                exchange=p.get("exchange"),
                trigger_id=int(p["trigger_id"]),
                trigger={
                    "kind": trig["kind"],
                    "price": str(trig["price"]) if trig.get("price") is not None else None,
                    "detail": trig.get("detail") or {},
                },
                brokers=deps.brokers,
                now=deps.now,
                dry_run=deps.dry_run,
                state=deps.state,
                session_runner=deps.session_runner or run_session,
                notifier=deps.notifier,
                cancel=deps.stop_event,
            )
        for tid in p.get("trigger_ids") or []:
            repo.mark_trigger_handled(
                conn, int(tid), resume.handled_marker(res), handled_at=deps.now().isoformat()
            )
    finally:
        conn.close()
    out = {"ticker": p["ticker"], "ok": res.ok, "session_id": res.session_id}
    if not res.ok:
        out["error"] = res.error or "재평가 세션 실패"
    return out


def _monitor_body(deps: DaemonDeps, market: str) -> dict:
    from trader.orchestrator import monitor as monitor_mod
    from trader.orchestrator.run_store import run_date_for
    from trader.orchestrator.session import run_session

    if deps.market_client is None:
        return {"skipped": "시세 클라이언트 없음"}
    hedge: dict | None = None
    try:
        hedge = _hedge_body(deps, market)
    except Exception as e:  # noqa: BLE001 - 헤지 실패가 손절 점검을 막지 않는다
        logger.exception("헤지 실행 실패 %s", market)
        deps.notifier.send(messages.failure("헤지 실패", f"{market}", e))
        hedge = {"error": f"{type(e).__name__}: {e}"}
    park: dict | None = None
    if market == "KR" and deps.settings.cash_park.enabled:
        try:
            park = _cash_park_body(deps)
        except Exception as e:  # noqa: BLE001 - 현금 운용 실패가 손절 점검을 막지 않는다
            logger.exception("현금 운용 실패")
            park = {"error": f"{type(e).__name__}: {e}"}
    rep: dict | None = None
    try:
        rep = _reprice_body(deps, market)
    except Exception as e:  # noqa: BLE001 - 재호가 실패가 손절 점검을 막지 않는다
        logger.exception("재호가 실행 실패 %s", market)
        deps.notifier.send(messages.failure("재호가 실패", f"{market}", e))
        rep = {"error": f"{type(e).__name__}: {e}"}
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
            enqueue_reevaluate=lambda params: _enqueue_reevaluate(deps, params),
            dry_run=deps.dry_run,
            state=deps.state,
            exchanges={u.symbol: u.exchange for u in (deps.universe.us if deps.universe else [])},
        )
        out = monitor_mod.monitor_once(ctx, market=market, today=run_date_for(market, deps.now()))
        if hedge and "skipped" not in hedge:
            out["hedge"] = hedge
        if park and "skipped" not in park:
            out["cash_park"] = park
        if rep and "skipped" not in rep:
            out["reprice"] = rep
        return out
    finally:
        conn.close()


def _rebalance_body(deps: DaemonDeps, market: str) -> dict:
    """주간 리밸런스(Phase 25 단계 7). 최근 결정 목표 비중 대비 밴드 밖 보유만, TRIM 먼저.
    게이트·계좌 모드·dry_run·paused 는 executor 가 그대로 본다."""
    from trader.orchestrator import executor, rebalance
    from trader.orchestrator.run_store import run_date_for

    if deps.market_client is None:
        return {"skipped": "시세 클라이언트 없음"}
    universe = deps.universe
    ex_cache: dict[str, str | None] = {}

    def exchange_of(t: str) -> str | None:
        # 유니버스 밖(발굴·제외된) US 보유도 브로커 보유의 거래소로 시세·주문을 낸다
        if market != "US":
            return None
        if t not in ex_cache:
            ex_cache[t] = _exchange_of(deps, market, t, universe)
        return ex_cache[t]

    def quote(t: str):
        if market == "KR":
            return deps.market_client.get_price_kr(t)  # price·change_pct(급등 추격 게이트)
        return deps.market_client.get_price_us(t, exchange_of(t) or "NAS")

    conn = deps.conn_factory()
    try:
        ectx = executor.ExecContext(
            settings=deps.settings,
            accounts=deps.accounts,
            conn=conn,
            brokers=deps.brokers,
            notifier=deps.notifier,
            state=deps.state,
            now=deps.now,
        )
        out = rebalance.run(
            ectx,
            market=market,
            today=run_date_for(market, deps.now()),
            dry_run=deps.dry_run,
            quote_fn=quote,
            exchange_of=exchange_of,
        )
    finally:
        conn.close()
    n = len(out.get("orders") or [])
    if n:
        deps.notifier.send(
            messages.event(
                "리밸런스", f"{market} 목표 비중 이탈 {len(out['legs'])}건 → 주문 시도 {n}건"
            )
        )
    return out


def _scalp_judge(deps: DaemonDeps):
    """판정자 스레드 풀은 데몬 수명 동안 하나(재시작이면 제출 중 판정은 버려진다 = 진입 안 함).
    판정 스레드는 원장 연결을 따로 연다(SQLite 연결은 스레드 사이에 공유하지 않는다)."""
    from trader.orchestrator import quick
    from trader.scalp import engine as scalp_engine

    if deps.scalp_judge is None:
        cfg = deps.settings.scalp

        def ask(payload: dict, schema: type):
            if deps.stop_event.is_set():  # 종료 중: 대기열에 남은 판정은 띄우지 않는다
                return None
            conn = deps.conn_factory()
            try:
                return quick.ask_json(
                    deps.settings,
                    conn,
                    agent="scalp-judge",
                    payload=payload,
                    schema=schema,
                    cap=cfg.judge_daily_cap,
                    timeout_s=cfg.judge_timeout_s,
                    now=deps.now,
                    cancel=deps.stop_event,  # 종료 요청이면 실행 중인 claude 도 정리
                )
            finally:
                conn.close()

        deps.scalp_judge = scalp_engine.ScalpJudge(ask)
    return deps.scalp_judge


def _theme_body(deps: DaemonDeps, market: str) -> dict:
    """테마 열기 감지 1회(Phase 25). 결과를 control·원장에 남기고 상위 테마가 바뀔 때만 알림."""
    from trader.orchestrator.run_store import run_date_for
    from trader.scan import theme
    from trader.sources import saveticker

    root = None
    if deps.sources_cfg is not None:
        root = saveticker.resolve_dir(deps.sources_cfg.news.saveticker_dir)
    conn = deps.conn_factory()
    try:
        heat = theme.compute(
            conn,
            deps.settings,
            market=market,
            today=run_date_for(market, deps.now()),
            now=deps.now(),
            market_client=deps.market_client,
            saveticker_root=root,
            state=deps.state,
        )
        msg = theme.record(conn, heat, top_n=deps.settings.theme.top_n)
    finally:
        conn.close()
    if msg:
        deps.notifier.send(msg)
    return {"themes": len(heat["themes"]), "alerted": bool(msg), **heat["signals"]}


def _scalp_job(deps: DaemonDeps, market: str) -> dict | None:
    """단타 잡: 창·일시정지·휴장(monitor_job)·꺼짐(_scalp_body)으로 tick 이 돌지 않았으면 그날
    모아 둔 진입·청산 묶음만 보낸다(전체 검토 T3 묶음 알림이 갇히지 않게)."""
    from trader.orchestrator.run_store import run_date_for
    from trader.scalp import engine as scalp_engine

    out = monitor_job(deps, market, lambda: _scalp_body(deps, market))
    if isinstance(out, dict) and out.get("skipped"):
        try:
            scalp_engine.flush_outbox(
                deps.state,
                run_date_for(market, deps.now()),
                market,
                notifier=deps.notifier,
                now=deps.now(),
                settings=deps.settings,
                dry_run=deps.dry_run,
            )
        except Exception:  # noqa: BLE001 - 알림 정리가 잡 결과를 바꾸지 않는다
            logger.warning("단타 묶음 정리 실패 %s", market, exc_info=True)
    return out


def _scalp_body(deps: DaemonDeps, market: str) -> dict:
    """분 단위 단타 1틱(Phase 23 S). 꺼져 있으면 skipped(잡 표에는 보인다). 창·paused 는 engine 이
    다시 보고, 세션·heavy 락과 무관하다."""
    from trader.orchestrator.run_store import run_date_for
    from trader.scalp import engine as scalp_engine

    if not deps.settings.scalp.enabled:
        return {"skipped": "scalp.enabled=false"}
    if deps.market_client is None:
        return {"skipped": "시세 클라이언트 없음"}
    ms = deps.schedule.kr if market == "KR" else deps.schedule.us
    conn = deps.conn_factory()
    try:
        ctx = scalp_engine.ScalpContext(
            settings=deps.settings,
            accounts=deps.accounts,
            conn=conn,
            brokers=deps.brokers,
            market_client=deps.market_client,
            open_hm=ms.monitor.start,
            close_hm=ms.monitor.end,
            notifier=deps.notifier,
            now=deps.now,
            state=deps.state,
            dry_run=deps.dry_run,
            exchanges={u.symbol: u.exchange for u in (deps.universe.us if deps.universe else [])},
            judge=_scalp_judge(deps) if deps.settings.scalp.judge else None,
        )
        return scalp_engine.tick(ctx, market=market, today=run_date_for(market, deps.now()))
    finally:
        conn.close()


def _balance_body(deps: DaemonDeps) -> dict:
    """정규장인 시장만 계좌 잔고를 다시 읽어 스냅샷 갱신(조회만, 주문 없음) → SSE `positions` 로
    화면의 순자산·보유·섹터가 새로고침 없이 바뀐다. 가벼운 잡(heavy 밖)."""
    from trader.orchestrator import balance

    if deps.stop_event.is_set():  # 일시정지는 거래 중단일 뿐 — 잔고 조회는 계속
        return {"skipped": "종료 중"}
    now = deps.now()
    markets = balance.open_markets(list(deps.settings.markets), now)
    if not markets:
        return {"skipped": "정규장 아님"}
    conn = deps.conn_factory()
    try:
        return {
            m: balance.refresh(
                conn,
                accounts=deps.accounts,
                brokers=deps.brokers,
                market=m,
                settings=deps.settings,
                now=now,
                market_client=deps.market_client,
                state=deps.state,
            )
            for m in markets
        }
    finally:
        conn.close()


def _usage_probe_body(deps: DaemonDeps) -> dict:
    """구독 사용률 확인(웹 사용량 블록 실시간). 최근 관측이 있거나 감속 hard 이상이면 건너뛴다."""
    from trader import usage
    from trader.orchestrator import quick

    cfg = deps.settings.llm.usage_probe
    if deps.stop_event.is_set() or not cfg.enabled:
        return {"skipped": "종료 중·꺼짐"}
    now = deps.now()
    conn = deps.conn_factory()
    try:
        # 원장(세션·확인)과 대화형 상태줄 파일을 합친 최신 관측 — 둘 중 하나라도 최근이면 쉰다
        obs = usage.latest_observation(conn, now, usage.usage_file(deps.settings.llm.usage_file))
        age = (obs or {}).get("age_s")
        if age is not None and age < cfg.skip_if_recent_min * 60:
            return {"skipped": f"최근 관측 {int(age)}초 전"}
        ok, reason = usage.session_allowance(conn, deps.settings, kind="resume", now=now)
        if not ok:
            return {"skipped": f"감속: {reason}"}
        return quick.probe_usage(deps.settings, conn, now=deps.now, cancel=deps.stop_event)
    finally:
        conn.close()


SAVETICKER_WATCH_S = 60


def _saveticker_body(deps: DaemonDeps) -> dict:
    """세이브티커 수집 서비스 감독(앱·데몬이 켜져 있는 동안 수집). 계정 파일이 있고 다른 서비스
    (예: launchd)가 같은 폴더를 돌리지 않으면 `trader saveticker service` 를 분리 실행하고, 죽으면
    다시 띄운다(인증 실패·재기동 한도는 `automation.should_start` 가 막는다). 장애·복구는 수집기
    감시(`automation.monitor`)가 판정해 텔레그램으로 한 번씩, 보관 기간 정리는 운영일 1회.
    가벼운 잡(heavy 밖) — 수집은 별도 프로세스라 데몬 스레드를 붙잡지 않는다."""
    from trader import saveticker as st
    from trader.saveticker import automation
    from trader.saveticker.store import Store
    from trader.sources import saveticker as reader

    news = deps.sources_cfg.news if deps.sources_cfg is not None else None
    home = reader.resolve_dir(news.saveticker_dir) if news is not None else None
    if news is None or not news.saveticker_collect or home is None:
        return {"skipped": "꺼짐"}
    if deps.stop_event.is_set():
        return {"skipped": "종료 중"}
    out: dict[str, Any] = {}
    proc = deps.saveticker_proc
    if proc is not None and proc.poll() is None:
        out["service"] = "실행 중"
    else:
        ok, reason = automation.should_start(home, st.credentials_path(), now=deps.now())
        if ok:
            automation.kill_leftovers(home)  # 죽은 서비스가 남긴 작업이 수집 잠금을 쥐고 있지 않게
            env = {
                **os.environ,
                "TRADER_HOME": str(paths.project_root()),
                "PYTHONIOENCODING": "utf-8",
            }
            deps.saveticker_proc = procs.spawn_detached(
                [*bundle.cli_argv(), "saveticker", "service"],
                log=home / "runtime" / "daemon-service.log",
                env=env,
            )
            deps.saveticker_home = home
            out["service"] = f"시작 pid {deps.saveticker_proc.pid}"
            logger.info("세이브티커 수집 서비스 시작 pid %s", deps.saveticker_proc.pid)
        else:
            out["service"] = reason
    if out["service"] != "계정 파일 없음":
        health = automation.monitor(
            home, send=lambda text: deps.notifier.send(messages.saveticker_health(text))
        )
        out["health"] = health.get("health")
    today = deps.now().astimezone(KST).date()
    if deps.saveticker_pruned != today and (home / "data").is_dir():
        deps.saveticker_pruned = today
        out["pruned"] = Store(home / "data").prune(
            today=today, keep_days=news.saveticker_retention_days
        )
    return out


def _backup_body(deps: DaemonDeps) -> dict:
    """원장 백업(매일 17:10 KST, Stock D-075): 사본 1개 + 보존 규칙 정리. 실패는 run_job 이 기록."""
    from trader.ledger import backup

    out = backup.backup(state=deps.state, now=deps.now())
    removed = backup.prune(out.parent, today=deps.now().astimezone(KST).date())
    return {"file": out.name, "pruned": removed}


def _ksd_body(deps: DaemonDeps) -> dict:
    """예탁원 권리 일정 수집(평일 07:05 KST) → control.ksd_events:KR. 시세 없으면 생략."""
    from trader.risk import blackout

    if deps.market_client is None or not hasattr(deps.market_client, "ksd_schedule"):
        return {"skipped": "시세 클라이언트 없음"}
    r = blackout.collect(
        deps.market_client, deps.settings.event_blackout, today=deps.now().astimezone(KST).date()
    )
    conn = deps.conn_factory()
    try:
        blackout.store(conn, r)
    finally:
        conn.close()
    return {"events": len(r["events"]), "errors": r["errors"]}


def _briefing_body(deps: DaemonDeps) -> dict:
    """아침 브리핑(평일 07:20 KST, KR 휴장일·일시정지 제외) → 텔레그램. 사람이 읽는 요약 전용."""
    from trader.orchestrator import briefing
    from trader.sources import saveticker

    root = None
    if deps.sources_cfg is not None:
        root = saveticker.resolve_dir(deps.sources_cfg.news.saveticker_dir)
    conn = deps.conn_factory()
    try:
        return briefing.run(
            conn,
            deps.settings,
            deps.accounts,
            now=deps.now(),
            notifier=deps.notifier,
            market_client=deps.market_client,
            saveticker_root=root,
            cancel=deps.stop_event,
        )
    finally:
        conn.close()


def _daily_report_body(deps: DaemonDeps, market: str) -> dict:
    """장 마감 일일 리포트 → 텔레그램(오늘 스냅샷이 있는 계좌만, 없으면 조용히). 먼저 체결을
    동기화한다 — US 마감(16:20 ET)은 정기 체결 동기화(07:10 KST)보다 앞서 실현손익·미결이 빈다."""
    from trader.ledger import daily_report
    from trader.orchestrator.run_store import run_date_for

    try:
        _fills_body(deps)
    except Exception as e:  # noqa: BLE001 - 동기화 실패여도 리포트는 원장 값으로
        logger.warning("일일 리포트 전 체결 동기화 실패 %s: %s", market, e)

    conn = deps.conn_factory()
    try:
        r = daily_report.compute(
            conn,
            deps.accounts,
            markets=[market],
            today_of=lambda m: run_date_for(m, deps.now()),
        )
    finally:
        conn.close()
    if r["items"]:
        deps.notifier.send(daily_report.message(r))
    return {"items": len(r["items"])}


def _paper_review_body(deps: DaemonDeps) -> dict:
    """모의 체결 대 가정 주간 점검(토 09:00 KST): 지난 7일 결정 주문 체결률·슬리피지 → 텔레그램."""
    from trader.ledger import paper_review

    conn = deps.conn_factory()
    try:
        r = paper_review.weekly(conn, deps.accounts, today=deps.now().astimezone(KST).date())
    finally:
        conn.close()
    if any(it["terminal"] for it in r["items"]) or r["alerts"]:
        deps.notifier.send(paper_review.message(r))
    return {"items": len(r["items"]), "alerts": len(r["alerts"])}


def _stop_saveticker(deps: DaemonDeps) -> None:
    """데몬 종료 때 이 데몬이 띄운 수집 서비스를 정리한다(서비스 → 수집 작업 → 브라우저 순)."""
    from trader.saveticker import automation

    proc, deps.saveticker_proc = deps.saveticker_proc, None
    if proc is None:
        return
    try:
        procs.stop_tree(proc, grace=automation.STOP_GRACE_S)
        if deps.saveticker_home is not None:  # 강제 종료로 끝났으면 작업·브라우저가 남는다
            automation.kill_leftovers(deps.saveticker_home)
    except Exception:  # noqa: BLE001 — 종료 경로는 막지 않는다
        logger.exception("세이브티커 수집 서비스 정리 실패")


def _ops_body(deps: DaemonDeps) -> dict:
    """운영 점검(Phase 23 A): 다섯 영역 판정 → 직전보다 나빠졌으면 알림 1건. 결과는
    `control.ops_result` 에 남겨 웹·텔레그램·STATUS 가 같은 것을 본다. 가벼운 잡(heavy 밖)."""
    from trader.ops import health

    conn = deps.conn_factory()
    try:
        result = health.check(
            conn,
            deps.settings,
            deps.accounts,
            now=deps.now(),
            state=deps.state,
            schedule_jobs=build_jobs(
                deps.schedule, deps.settings, markets=list(deps.settings.markets)
            ),
        )
        control.set_flag(conn, health.RESULT_FLAG, result)
        msg = health.escalate(conn, result)
        if msg:
            deps.notifier.send(msg)
        return {"level": result["level"], "items": len(result["items"]), "notified": bool(msg)}
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
            if b is None:
                continue
            try:
                # US 체결은 ET 날짜로 조회해야 한다 (07:10 KST 는 전날 ET) → 시장별 현지 날짜
                out[a.id] = sync_fills(conn, b, a, deps.now(), notifier=deps.notifier)
            except Exception as e:  # noqa: BLE001 - 한 계좌의 실패가 다른 계좌 동기화를 막지 않는다
                logger.warning("체결 동기화 실패 %s: %s", a.id, e)
                out[a.id] = {"error": f"{type(e).__name__}: {e}"}
    finally:
        conn.close()
    return out


def _reconcile_body(deps: DaemonDeps, market: str | None) -> dict:
    """야간 KIS 대조(Phase 21): 계좌·시장별 브로커 보유 vs 원장 스냅샷, 마감 뒤 미정리 주문.
    불일치는 알림 + 스냅샷 보정. 가벼운 잡."""
    from trader.orchestrator import reconcile
    from trader.orchestrator.run_store import run_date_for

    conn = deps.conn_factory()
    try:
        return reconcile.reconcile_all(
            conn,
            deps.brokers,
            deps.accounts,
            market=market,
            today_for=lambda m: run_date_for(m, deps.now()),
            now=deps.now,
            notifier=deps.notifier,
            label=lambda m, t: names.display(conn, _cache_dir(deps), m, t),
        )
    finally:
        conn.close()


def _news_body(deps: DaemonDeps) -> dict:
    """시장 전체 뉴스 저장소 수집(Phase 19): 시장마다 질의 묶음을 돌려 news_items 에 쌓고 보존
    기간을 넘긴 행을 지운다. 한 시장의 실패가 다른 시장을 막지 않는다. 가벼운 잡(heavy 락 밖)."""
    from trader.config import load_universe
    from trader.sources import news_store

    if deps.sources_cfg is None or not deps.sources_cfg.news.enabled:
        return {"skipped": "news.enabled=false"}
    cfg = deps.sources_cfg.news
    universe = deps.universe or load_universe()
    cache_names = names.load_cache_names(_cache_dir(deps))
    out: dict[str, Any] = {}
    conn = deps.conn_factory()
    try:
        for market in deps.settings.markets:
            if deps.stop_event.is_set():
                break
            try:
                out[market] = news_store.collect(
                    conn,
                    cfg,
                    deps.secrets or Secrets(),
                    market=market,
                    names=news_store.universe_names(universe, cache_names, market),
                    now=deps.now,
                )
            except Exception as e:  # noqa: BLE001 - 시장 단위 격리
                logger.warning("뉴스 수집 실패 %s: %s", market, e)
                out[market] = {"error": f"{type(e).__name__}: {e}"}
        try:
            from trader.sources import saveticker

            root = saveticker.resolve_dir(cfg.saveticker_dir)
            out["saveticker"] = (
                saveticker.ingest_news(
                    conn,
                    root,  # type: ignore[arg-type]
                    now=deps.now,
                    names_by_market={
                        m: news_store.universe_names(universe, cache_names, m)
                        for m in deps.settings.markets
                    },
                )
                if root is not None
                else {"skipped": "saveticker_dir 미설정"}
            )
        except Exception as e:  # noqa: BLE001 - 로컬 폴더 문제가 뉴스 잡을 막지 않는다
            logger.warning("세이브티커 적재 실패: %s", e)
            out["saveticker"] = {"error": f"{type(e).__name__}: {e}"}
        for market in deps.settings.markets:  # 공시 중요도(Phase 25): 수집 → critical 즉시 처리
            try:
                out[f"disclosure:{market}"] = _disclosure_trigger(deps, conn, market, universe)
            except Exception as e:  # noqa: BLE001 - 공시 실패가 뉴스 수집을 막지 않는다
                logger.warning("공시 처리 실패 %s: %s", market, e)
                out[f"disclosure:{market}"] = {"error": f"{type(e).__name__}: {e}"}
        out["pruned"] = news_store.prune(
            conn, before=deps.now() - timedelta(days=cfg.store_retention_days)
        )
        for market in deps.settings.markets:
            try:
                out[f"trigger:{market}"] = _news_trigger(deps, conn, market, universe)
            except Exception as e:  # noqa: BLE001 - 트리거 실패가 수집 결과를 바꾸지 않는다
                logger.warning("뉴스 트리거 실패 %s: %s", market, e)
                out[f"trigger:{market}"] = {"error": f"{type(e).__name__}: {e}"}
    finally:
        conn.close()
    return out


def _in_market_window(deps: DaemonDeps, market: str) -> bool:
    ms = deps.schedule.kr if market == "KR" else deps.schedule.us
    now_local = deps.now().astimezone(ZoneInfo(ms.tz))
    return in_window(
        now_local.time().replace(second=0, microsecond=0), ms.monitor.start, ms.monitor.end
    )


def _held_tickers(deps: DaemonDeps, market: str) -> set[str]:
    held: set[str] = set()
    for a in deps.accounts:
        b = deps.brokers.get(a.id)
        if market in a.markets and b is not None:
            try:
                held |= {p.ticker for p in b.get_positions(market)}
            except Exception as e:  # noqa: BLE001
                logger.debug("보유 조회 실패 %s: %s", a.id, e)
    return held


DISCLOSURE_SEEN_MAX = 300


def _disclosure_trigger(deps: DaemonDeps, conn: sqlite3.Connection, market: str, universe) -> dict:
    """공시 중요도(Phase 25 단계 6). 보유·유니버스·관심 종목의 오늘 공시를 적재하고, 처음 보는
    critical 은 `[중요 공시]` 알림 + 새 뉴스 건수 조건(news_min_novel·news_burst) 없이 즉시 재분석
    요청(paused·장중 창 밖이면 알림만). 같은 공시는 `control.disclosure_seen:<market>` 로 1회."""
    from trader.orchestrator.run_store import run_date_for
    from trader.sources import disclosures

    uni = set(universe.kr) if market == "KR" else {u.symbol for u in universe.us}
    held = _held_tickers(deps, market)
    watch = set(control.get_watchlist(conn, market=market))
    inp = deps.input_deps
    res = disclosures.collect(
        conn,
        market=market,
        tickers=uni | held | watch,
        now=deps.now(),
        dart=getattr(inp, "dart", None),
        edgar=getattr(inp, "edgar", None),
    )
    key = f"disclosure_seen:{market}"
    seen = list(control.get_flag(conn, key) or [])
    fresh = [
        r
        for r in disclosures.critical_recent(
            conn, market, since=deps.now() - timedelta(days=1), tickers=uni | held
        )
        if r["id"] not in seen
    ]
    if not fresh:
        return res
    can_run = not control.is_paused(conn) and _in_market_window(deps, market)
    today = run_date_for(market, deps.now())
    cache = _cache_dir(deps)
    hedge = deps.settings.excluded_tickers(market)
    queued = []
    for r in fresh:
        for t in r["tickers"]:
            if t in hedge or t not in (uni | held):
                continue
            rid = _news_reanalyze(deps, conn, market, t, [r], today, universe) if can_run else None
            action = (
                f"재분석 요청 #{rid}"
                if rid is not None
                else ("재분석 요청 중복" if can_run else "일시정지·장 밖 — 알림만")
            )
            if rid is not None:
                queued.append({"ticker": t, "request_id": rid})
            try:
                deps.notifier.send(
                    messages.disclosure(
                        market=market,
                        label=names.display(conn, cache, market, t),
                        title=r["title"],
                        url=r["url"],
                        action=action,
                    )
                )
            except Exception:  # noqa: BLE001
                logger.debug("중요 공시 알림 실패", exc_info=True)
        seen.append(r["id"])
    control.set_flag(conn, key, seen[-DISCLOSURE_SEEN_MAX:])
    return res | {"critical_new": len(fresh), "queued": queued}


def _news_trigger(deps: DaemonDeps, conn: sqlite3.Connection, market: str, universe) -> dict:
    """장중 뉴스 발굴(Phase 23). 수집 뒤 새 내용 뉴스(`novel_items`)를 판정해 ① 유니버스·보유 종목에
    붙은 새 뉴스 → 그 종목 재분석(오늘 결정 있으면 재평가 트리거 kind `news`, 없으면
    analyze_ticker),
    ② 새 뉴스가 1건 이상이면 scout 세션(큐 kind `discover`)으로 유니버스 밖 발굴. 유사 기사만이면
    아무 세션도 띄우지 않는다. 장중 창·paused·설정이 조건."""
    from trader.orchestrator.run_store import run_date_for
    from trader.scan import discovery as discovery_mod
    from trader.sources import news_store

    cfg = deps.settings.discovery.intraday
    key = f"news_trigger_at:{market}"
    since_raw = control.get_flag(conn, key)
    since = datetime.fromisoformat(since_raw) if since_raw else deps.now() - timedelta(hours=1)
    novel = news_store.novel_items(
        conn,
        market,
        since=since,
        lookback_days=cfg.similarity.lookback_days,
        jaccard=cfg.similarity.jaccard,
        ratio=cfg.similarity.ratio,
    )
    control.set_flag(conn, key, deps.now().isoformat())
    out: dict[str, Any] = {"novel": len(novel), "queued": [], "discover": None}
    if not novel:
        return out
    if not cfg.enabled:
        return out | {"skipped": "discovery.intraday.enabled=false"}
    if control.is_paused(conn):
        return out | {"skipped": "일시정지"}
    if not _in_market_window(deps, market):
        return out | {"skipped": "장중 창 밖 (아침 scan 이 48시간 뉴스를 본다)"}
    today = run_date_for(market, deps.now())
    uni = set(universe.kr) if market == "KR" else {u.symbol for u in universe.us}
    held: set[str] = set()
    for a in deps.accounts:
        b = deps.brokers.get(a.id)
        if market in a.markets and b is not None:
            try:
                held |= {p.ticker for p in b.get_positions(market)}
            except Exception as e:  # noqa: BLE001
                logger.debug("보유 조회 실패(뉴스 트리거) %s: %s", a.id, e)
    burst = discovery_mod.news_burst(
        novel, universe_tickers=uni, held=held, min_items=cfg.min_items
    )
    hedge = deps.settings.excluded_tickers(market)
    for ticker, items in burst["tickers"].items():
        if ticker in hedge:
            continue
        if ticker in held:  # 보유 종목은 보유기간 프로필의 news_min_novel 을 본다(Phase 23 T)
            from trader.ledger import repo
            from trader.risk import horizon

            latest = repo.latest_decision(conn, market, ticker)
            _, prof = horizon.of_decision(deps.settings.risk, (latest or {}).get("data"))
            if len(items) < prof.news_min_novel:
                out.setdefault("below_profile", []).append(
                    {"ticker": ticker, "news": len(items), "need": prof.news_min_novel}
                )
                continue
        rid = _news_reanalyze(deps, conn, market, ticker, items, today, universe)
        if rid is not None:
            out["queued"].append({"ticker": ticker, "request_id": rid, "news": len(items)})
    cap_key = f"discover_count:{market}:{today.isoformat()}"
    n_disc = int(control.get_flag(conn, cap_key) or 0)
    if n_disc < cfg.discover_cap:
        try:
            rid = jobq.enqueue(conn, "discover", {"market": market}, source="news", now=deps.now())
            control.set_flag(conn, cap_key, n_disc + 1)
            out["discover"] = rid
        except jobq.DuplicateRequest:
            out["discover"] = "pending"
    else:
        out["discover"] = f"상한 {cfg.discover_cap}"
    if out["queued"] or isinstance(out["discover"], int):
        cache = _cache_dir(deps)
        parts = [
            f"{names.display(conn, cache, market, q['ticker'])} 새 뉴스 {q['news']}건"
            f" → #{q['request_id']}"
            for q in out["queued"]
        ]
        if isinstance(out["discover"], int):
            parts.append(f"유니버스 밖 발굴 세션 #{out['discover']}")
        try:
            deps.notifier.send(
                messages.event("뉴스 발굴", f"{market} 새 뉴스 {len(novel)}건", *parts)
            )
        except Exception:  # noqa: BLE001
            logger.debug("뉴스 발굴 알림 실패", exc_info=True)
    return out


def _exchange_of(deps: DaemonDeps, market: str, ticker: str, universe) -> str | None:
    if market != "US":
        return None
    for u in getattr(universe, "us", []) or []:
        if u.symbol == ticker:
            return u.exchange
    for a in deps.accounts:
        b = deps.brokers.get(a.id)
        if b is None or market not in a.markets:
            continue
        try:
            for p in b.get_positions(market):
                if p.ticker == ticker and getattr(p, "exchange", None):
                    return p.exchange
        except Exception:  # noqa: BLE001
            continue
    return None


def _news_reanalyze(
    deps: DaemonDeps,
    conn: sqlite3.Connection,
    market: str,
    ticker: str,
    items: list[dict],
    today: date,
    universe,
    *,
    exchange: str | None = None,
) -> int | None:
    """새 뉴스가 붙은 종목의 재분석 요청: 오늘 `30_decision` 이 있으면 트리거 kind `news` + 큐
    `reevaluate`(31_decision_r<n>), 없으면 큐 `analyze_ticker`. 중복 요청은 None."""
    from trader.ledger import repo

    exchange = exchange or _exchange_of(deps, market, ticker, universe)
    if market == "US" and not exchange:
        logger.warning("뉴스 재분석 생략 %s %s: 거래소 코드 없음", market, ticker)
        return None
    detail = {
        "news": [
            {"id": n.get("id"), "title": n.get("title"), "source": n.get("source")}
            for n in items[:10]
        ],
        "count": len(items),
    }
    try:
        if repo.decision_id(conn, today, market, ticker) is not None:
            tid = repo.insert_trigger(
                conn,
                market=market,
                ticker=ticker,
                kind="news",
                detail=detail,
                price=None,
                decision_id=repo.decision_id(conn, today, market, ticker),
                created_at=deps.now().isoformat(),
            )
            return jobq.enqueue(
                conn,
                "reevaluate",
                {"market": market, "ticker": ticker, "exchange": exchange, "trigger_id": tid},
                source="news",
                now=deps.now(),
            )
        params: dict[str, Any] = {"market": market, "ticker": ticker, "dry_run": deps.dry_run}
        if exchange:
            params["exchange"] = exchange
        return jobq.enqueue(
            conn, "analyze_ticker", params, source="news", now=deps.now(), priority=5
        )
    except jobq.DuplicateRequest:
        return None


def _discover_body(deps: DaemonDeps, market: str) -> dict:
    """장중 발굴 세션(Phase 23): 새 내용 뉴스만 실은 `_discovery_inputs` → scout 1단계 세션
    (kind=discover) → `_discovery` 후보를 재분석 큐로(오늘 결정 있으면 재평가). 세션 상한은
    `run_session` 이 본다."""
    from trader.config import load_sources, load_universe
    from trader.orchestrator.run_store import read_checkpoint, run_date_for
    from trader.orchestrator.session import SessionSpec, run_session
    from trader.scan import discovery as discovery_mod
    from trader.sources import news_store

    _close_stale_sessions(deps)
    universe = deps.universe or load_universe()
    today = run_date_for(market, deps.now())
    conn = deps.conn_factory()
    try:
        lo = (deps.now() - timedelta(hours=6)).astimezone(UTC).isoformat()
        rows = conn.execute(
            "SELECT id, market, source, query, title, url, url_hash, published_at, received_at, "
            "summary, tickers_json, lang FROM news_items WHERE market=? AND novel=1 "
            "AND received_at >= ? ORDER BY received_at DESC LIMIT ?",
            (market, lo, 60),
        ).fetchall()
        novel = [news_store._row(r) for r in rows]
        if not novel:
            return {"skipped": "새 뉴스 없음"}
        analyzed = [
            r["ticker"]
            for r in conn.execute(
                "SELECT ticker FROM decisions WHERE date=? AND market=?",
                (today.isoformat(), market),
            ).fetchall()
        ]
        held: list[str] = []
        for a in deps.accounts:
            b = deps.brokers.get(a.id)
            if market in a.markets and b is not None:
                try:
                    held += [p.ticker for p in b.get_positions(market)]
                except Exception:  # noqa: BLE001
                    pass
        try:
            news_cfg = load_sources().news
        except Exception:  # noqa: BLE001
            news_cfg = None
        discovery_mod.build_inputs(
            conn,
            deps.settings,
            universe,
            market=market,
            today=today,
            asof=deps.now(),
            quant_candidates=[],
            held=held,
            state=deps.state,
            now=deps.now,
            news_cfg=news_cfg,
            cache_names=names.load_cache_names(_cache_dir(deps)),
            sources_cfg=deps.sources_cfg,
            http=getattr(deps.input_deps, "http", None),
            intraday_news=novel,
            analyzed_today=analyzed,
        )
        runner = deps.session_runner or run_session
        res = runner(
            SessionSpec(
                market=market,
                date=today,
                tickers=[],
                dry_run=True,
                asof=deps.now(),
                kind="discover",
            ),
            deps.settings,
            conn,
            deps.accounts,
            state=deps.state,
            now=deps.now,
            brokers=deps.brokers,
            notifier=deps.notifier,
            cancel=deps.stop_event,
        )
        if not res.ok:
            return {"error": f"discover 세션 실패: {res.error}", "novel": len(novel)}
        found = (
            read_checkpoint(run_store.market_dir(today, market, deps.state), "_discovery") or {}
        ).get("data") or {}
        queued = []
        for c in found.get("candidates") or []:
            t = c.get("ticker")
            if not t or t in deps.settings.excluded_tickers(market):
                continue
            rid = _news_reanalyze(
                deps, conn, market, t, [], today, universe, exchange=c.get("exchange")
            )
            if rid is not None:
                queued.append({"ticker": t, "request_id": rid, "tier": c.get("tier")})
        if queued:
            deps.notifier.send(
                messages.event(
                    "장중 발굴",
                    market,
                    *(f"{q['ticker']}({q['tier']}) → 요청 #{q['request_id']}" for q in queued),
                )
            )
        return {
            "novel": len(novel),
            "candidates": len(found.get("candidates") or []),
            "queued": queued,
        }
    finally:
        conn.close()


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
            deps.notifier.send(
                messages.event("피처 스냅샷 실패", f"{e} — 만기 결과가 채워지지 않을 수 있어요")
            )
    conn = deps.conn_factory()
    try:
        matured = outcomes.mature(conn, state=deps.state, today=_today(deps))
        views_matured = _mature_market_views(deps, conn)
        for m in deps.settings.markets:  # 테마 열기 판정 사후 검증(Phase 25) — 실패는 경고만
            try:
                from trader.scan import theme as theme_mod

                theme_mod.mature(conn, m, state=deps.state, today=_today(deps))
            except Exception as e:  # noqa: BLE001
                logger.warning("테마 판정 채점 실패 %s: %s", m, e)
        try:
            from trader.ledger import shadow

            index_tables = {
                m: shadow.index_table(deps.market_client, deps.settings.hedge, m)
                for m in deps.settings.markets
            }
            shadow_matured = shadow.mature(
                conn, state=deps.state, today=_today(deps), index_tables=index_tables
            )
        except Exception as e:  # noqa: BLE001 - 섀도 만기 실패가 반성을 막지 않는다
            logger.warning("섀도 포트폴리오 만기 실패: %s", e)
            shadow_matured = None
        sessions = 0
        errors: list[str] = []
        for market in deps.settings.markets:
            if not pending(conn, market=market):
                continue
            if deps.stop_event.is_set():
                logger.warning("종료 중: reflect 세션 생략 %s", market)
                break
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
                    cancel=deps.stop_event,
                )
            except Exception as e:  # noqa: BLE001 - 한 시장의 실패가 다른 시장을 막지 않는다
                logger.exception("reflect 실패 %s", market)
                errors.append(f"{market}: {type(e).__name__}: {e}")
                deps.notifier.send(messages.event("복기 실패", f"{market}: {e}"))
                continue
            if res.session_id or res.fake:
                sessions += 1
            if not res.ok:
                errors.append(f"{market}: {res.error}")
                deps.notifier.send(messages.event("복기 실패", f"{market}: {res.error}"))
        pr = store.promote_and_prune(conn, now=deps.now(), settings=deps.settings.memory)
        try:  # 보존 정리(전체 검토 P12) — 실패가 반성 결과를 바꾸지 않는다
            from trader.ops import retention

            retained = retention.prune(
                conn, deps.state, today=_today(deps), keep_days=deps.settings.retention_days
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("보존 정리 실패: %s", e)
            retained = None
    finally:
        conn.close()
    return {
        "features": features_n,
        "matured": len(matured),
        "views_matured": views_matured,
        "shadow_matured": shadow_matured,
        "sessions": sessions,
        "errors": errors,
        "pruned": pr["deleted"],
        "retention": retained,
    }


def _ml_train_body(deps: DaemonDeps, *, market: str | None = None) -> dict:
    from trader.config import load_ml
    from trader.ml import train as train_mod

    try:
        r = train_mod.train_quant(load_ml(), state=deps.state, now=deps.now, market=market)
    except Exception as e:  # noqa: BLE001 - 학습 실패는 통보하고 데몬은 계속
        logger.exception("ML 학습 실패")
        deps.notifier.send(messages.failure("ML 학습 실패", "", e))
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    met = r.get("metrics") or {}
    acc = (
        f"정확도 {met['accuracy']:.3f} / 기준선 {met['baseline']:.3f}"
        if met
        else r.get("promote_reason")
    )
    deps.notifier.send(
        messages.ml_trained(
            kind="퀀트",
            version=r.get("version"),
            trained=r.get("status") == "trained",
            detail=acc,
            promoted=bool(r.get("promoted")) if r.get("status") == "trained" else None,
        )
    )
    rl = _rl_train(deps, market=market)
    if rl is not None:
        r["rl_sizer"] = rl
    return r


def _rl_train(
    deps: DaemonDeps, *, market: str | None = None, seeds: int | None = None
) -> dict | None:
    """사이저 학습(승격은 `rl promote` 수동). `rl_sizer.enabled` 는 적용 스위치이지 학습 스위치가
    아니라 주간 학습은 늘 돈다. 데이터 부족은 로그만, 실패는 통보."""
    from trader.config import load_ml
    from trader.ml.rl import train as rl_train_mod

    try:
        cfg = load_ml()
        if seeds is not None:
            rl_cfg = cfg.models.rl_sizer.model_copy(update={"seeds": seeds})
            cfg = cfg.model_copy(
                update={"models": cfg.models.model_copy(update={"rl_sizer": rl_cfg})}
            )
        conn = deps.conn_factory()
        try:
            r = rl_train_mod.train_sizer(cfg, conn, state=deps.state, now=deps.now, market=market)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 - 실험 학습 실패가 데몬을 막지 않는다
        logger.exception("RL 사이저 학습 실패")
        deps.notifier.send(messages.failure("RL 학습 실패", "", e))
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    if r.get("status") == "insufficient_data":
        logger.info("RL 사이저 학습 생략: %s", r.get("promote_reason") or "데이터 부족")
        return r
    s = (r.get("summary") or {}).get("rl") or {}
    deps.notifier.send(
        messages.ml_trained(
            kind="RL 사이저",
            version=r.get("version"),
            trained=r.get("status") == "trained",
            detail=f"RL 수익 평균 {s.get('return_mean', '-')}",
            promoted=None,
            note="승격은 `trader rl promote` 로 수동이에요.",
        )
    )
    return r


def _scan_analyze_body(deps: DaemonDeps, market: str) -> dict:
    """scan 뒤 바로 analyze. scan 이 예외로 죽어도 통보하고 analyze 를 시도한다(_analyze_body 가
    `_scan.json` 없음을 알림·error 로 남긴다). 세션을 세기 전에 stale running 을 정리한다."""
    _close_stale_sessions(deps)
    _regime_body(deps, market)  # 국면 갱신: 스크리너·트레이더 입력(context.regime)·게이트가 읽는다
    try:
        _market_view_body(deps, market)  # 시장 세션(Phase 18): 헤지 비중 제안, 실패해도 계속
    except Exception as e:  # noqa: BLE001 - 시장 세션 실패가 스캔을 막지 않는다
        logger.exception("시장 세션 실패 %s", market)
        deps.notifier.send(messages.failure("시장 세션 실패", f"{market}", e))
    try:
        _regime_apply_view(deps, market)  # Phase 19: 전략가 RISK_OFF 를 4번째 지표로 재판정
    except Exception as e:  # noqa: BLE001
        logger.exception("전략가 지표 반영 실패 %s", market)
        deps.notifier.send(messages.failure("국면 재판정 실패", f"{market}", e))
    scan_error: str | None = None
    try:
        _scan_body(deps, market)
    except Exception as e:  # noqa: BLE001 - scan 실패가 analyze 시도를 막지 않는다
        logger.exception("scan 실패 %s", market)
        scan_error = f"{type(e).__name__}: {e}"  # 알림은 _analyze_body 가 원인과 함께 1건만
    out = _analyze_body(deps, market, scan_error=scan_error)
    _shadow_record(deps, market, out)
    return out


def _shadow_record(deps: DaemonDeps, market: str, analyzed: dict | None = None) -> None:
    """섀도 포트폴리오(전체 검토 L1): 오늘 scan 후보·LLM 결정으로 비교 포트폴리오 3개를 남긴다.
    주문 없음, 실패해도 잡은 계속(경고만). 분석이 끝까지 돌지 않은 날(실패·중단·scan 없음)과
    FAKE 세션은 남기지 않는다 — 운영 실패나 고정 HOLD 를 LLM 선택으로 채점하지 않게(PR #33)."""
    from trader.ledger import shadow
    from trader.orchestrator import session as session_mod
    from trader.orchestrator.run_store import read_checkpoint, run_date_for

    a = analyzed or {}
    if a.get("failed") or a.get("stopped") or a.get("error"):
        logger.info("섀도 기록 생략 %s: 분석 미완(%s)", market, a)
        return
    today = run_date_for(market, deps.now())
    scan = read_checkpoint(run_store.market_dir(today, market, deps.state), "_scan")
    if not scan or session_mod.is_fake() or ((scan.get("session") or {}).get("fake")):
        return
    try:
        conn = deps.conn_factory()
        try:
            index = shadow.index_table(deps.market_client, deps.settings.hedge, market)
            shadow.record(conn, market=market, day=today, scan=scan, now=deps.now(), index=index)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 - 측정 실패가 분석 잡을 실패로 만들지 않는다
        logger.warning("섀도 포트폴리오 기록 실패 %s: %s", market, e)


def _cache_dir(deps: DaemonDeps) -> Path:
    return (deps.state or paths.state_dir()) / "cache"


def _ticker_lock(deps: DaemonDeps, market: str, ticker: str) -> threading.Lock:
    """(시장, 종목) 결정 실행 락. analyze 세션과 재평가 세션이 같은 종목에서 겹치지 않게 한다."""
    with deps.ticker_locks_guard:
        return deps.ticker_locks.setdefault((market, str(ticker)), threading.Lock())


def _heavy(deps: DaemonDeps, fn: Callable[[], Any], *, cron: bool = True) -> Any:
    """무거운 잡은 한 번에 하나. cron 은 기다리되 `heavy_wanted` 를 세워 대기열 잡이 양보하게
    하고, HEAVY_WAIT_NOTIFY_S 넘게 기다리면 알림 1건을 보낸다."""
    if cron:
        deps.heavy_wanted.enter()
    waited = 0.0
    notified = False
    try:
        while not deps.heavy_lock.acquire(timeout=HEAVY_POLL_S):
            waited += HEAVY_POLL_S
            if deps.stop_event.is_set():
                return {"skipped": "종료 중"}
            if waited >= HEAVY_WAIT_NOTIFY_S and not notified:
                notified = True
                logger.warning("cron 잡이 heavy 락을 %.0f분 기다리는 중", waited / 60)
                try:
                    deps.notifier.send(
                        messages.event(
                            "잡 대기",
                            f"예정된 잡이 다른 무거운 잡(백필·학습) 때문에 "
                            f"{waited / 60:.0f}분 기다리고 있어요",
                        )
                    )
                except Exception:  # noqa: BLE001
                    logger.debug("대기 알림 실패", exc_info=True)
    finally:
        if cron:
            deps.heavy_wanted.leave()
    try:
        return fn()
    finally:
        deps.heavy_lock.release()


def _yield_heavy(deps: DaemonDeps) -> None:
    """대기열 잡(백필)이 종목 경계에서 부른다: cron 이 기다리면 heavy 락을 내놓고 cron 이 끝난 뒤
    다시 잡는다. 락을 들고 있지 않으면 아무것도 하지 않는다."""
    if not deps.heavy_wanted.is_set() or not deps.heavy_lock.locked():
        return
    logger.info("대기열 잡이 예정 잡에 양보 (heavy 락 반납)")
    deps.heavy_lock.release()
    try:
        while deps.heavy_wanted.is_set() and not deps.stop_event.is_set():
            deps.stop_event.wait(0.05)
    finally:
        deps.heavy_lock.acquire()
    logger.info("대기열 잡 재개")


def job_body(
    deps: DaemonDeps, job: Job, *, heartbeat: Heartbeat | None = None
) -> Callable[[], Any]:
    if job.kind == "scan_analyze":
        return lambda: market_job(
            deps,
            job.market or "KR",
            "scan_analyze",
            lambda: _heavy(deps, lambda: _scan_analyze_body(deps, job.market or "KR")),
        )
    if job.kind == "monitor":
        return lambda: monitor_job(
            deps, job.market or "KR", lambda: _monitor_body(deps, job.market or "KR")
        )
    if job.kind == "scalp":
        return lambda: _scalp_job(deps, job.market or "KR")
    if job.kind == "rebalance":
        return lambda: market_job(
            deps, job.market or "KR", "rebalance", lambda: _rebalance_body(deps, job.market or "KR")
        )
    if job.kind == "ksd_events":
        return lambda: _ksd_body(deps)
    if job.kind == "briefing":
        return lambda: market_job(deps, "KR", "briefing", lambda: _briefing_body(deps))
    if job.kind == "daily_report":
        return lambda: market_job(
            deps,
            job.market or "KR",
            "daily_report",
            lambda: _daily_report_body(deps, job.market or "KR"),
        )
    if job.kind == "theme":
        return lambda: monitor_job(
            deps, job.market or "KR", lambda: _theme_body(deps, job.market or "KR")
        )
    if job.kind == "telegram_poll":
        return lambda: _telegram_body(deps)
    if job.kind == "fills_sync":
        return lambda: _fills_body(deps)
    if job.kind == "news_collect":
        return lambda: _news_body(deps)
    if job.kind == "ops_check":
        return lambda: _ops_body(deps)
    if job.kind == "balance_refresh":
        return lambda: _balance_body(deps)
    if job.kind == "usage_probe":
        return lambda: _usage_probe_body(deps)
    if job.kind == "saveticker":
        return lambda: _saveticker_body(deps)
    if job.kind == "ledger_backup":
        return lambda: _backup_body(deps)
    if job.kind == "paper_review":
        return lambda: _paper_review_body(deps)
    if job.kind == "reconcile":
        return lambda: market_job(
            deps, job.market or "KR", "reconcile", lambda: _reconcile_body(deps, job.market)
        )
    if job.kind == "reflect":
        return lambda: _heavy(deps, lambda: _reflect_body(deps))
    if job.kind == "ml_train":
        return lambda: _heavy(deps, lambda: _ml_train_body(deps))
    if job.kind == "job_queue":
        return lambda: _queue_tick(deps, heartbeat)
    if job.kind == "job_queue_fast":
        return lambda: _fast_queue_tick(deps, heartbeat)
    raise ValueError(f"알 수 없는 잡: {job.kind}")


# --- 작업 대기열 (jobs.py) ------------------------------------------------------------


def _progress_writer(deps: DaemonDeps, req_id: int) -> Callable[[str], None]:
    """진행 문구를 1초에 한 번만 DB 에 적는다(종목마다 UPDATE 하지 않도록). 종료 신호가 오면
    `JobCancelled` 로 백필이 종목 경계에서 끊긴다."""
    state = {"n": 0, "last": 0.0}

    def write(text: str) -> None:
        state["n"] += 1
        if deps.stop_event.is_set():
            raise jobq.JobCancelled("종료 중")
        _yield_heavy(deps)  # cron 이 기다리면 종목 경계에서 양보
        now = datetime.now(UTC).timestamp()
        if now - state["last"] < 1.0:
            return
        state["last"] = now
        conn = deps.conn_factory()
        try:
            jobq.set_progress(conn, req_id, f"[{state['n']}] {text}")
        finally:
            conn.close()

    return write


def _features_action(deps: DaemonDeps, action: str, p: dict, progress: Callable) -> dict:
    if deps.features_fn is None:
        return {"error": "시세 클라이언트 없음(피처 작업 불가)"}
    markets = [p["market"]] if p.get("market") else list(deps.settings.markets)
    kw = {"years": p["years"]} if action == "backfill" else {}
    n = deps.features_fn(action, markets, progress=progress, **kw)
    return {"rows": int(n)}


def _analyze_ticker_request(deps: DaemonDeps, p: dict) -> dict:
    from trader.orchestrator.run_store import run_date_for

    market, ticker, exchange = p["market"], p["ticker"], p.get("exchange")
    if market == "US" and not exchange:
        for u in deps.universe.us if deps.universe else []:
            if u.symbol == ticker:
                exchange = u.exchange
                break
        if not exchange:
            return {"error": f"미국 종목은 거래소 코드가 필요해요: {ticker}"}
    _close_stale_sessions(deps)
    today = run_date_for(market, deps.now())
    return _analyze_targets(
        deps,
        market,
        [{"ticker": ticker, "exchange": exchange}],
        today=today,
        dry_run=bool(p.get("dry_run", True)),
    )


def queued_job_body(
    deps: DaemonDeps, req: dict, progress: Callable[[str], None]
) -> Callable[[], Any]:
    """대기열 요청 → 기존 잡 본체. 사용자가 명시적으로 눌렀으므로 주말·휴장 게이트는 없다."""
    kind, p = req["kind"], req["params"]
    if kind == "scan_analyze":
        return lambda: _scan_analyze_body(deps, p["market"])
    if kind == "market_view":
        return lambda: _market_view_body(deps, p["market"])
    if kind == "reevaluate":
        return lambda: _reevaluate_request(deps, p)
    if kind == "analyze_ticker":
        return lambda: _analyze_ticker_request(deps, p)
    if kind in ("features_backfill", "features_snapshot", "features_derive"):
        action = kind.split("_", 1)[1]
        return lambda: _features_action(deps, action, p, progress)
    if kind == "ml_train":
        return lambda: _ml_train_body(deps, market=p.get("market"))
    if kind == "rl_train":
        return lambda: _rl_train(deps, market=p.get("market"), seeds=p.get("seeds")) or {}
    if kind == "reflect":
        return lambda: _reflect_body(deps)
    if kind == "fills_sync":
        return lambda: _fills_body(deps)
    if kind == "news_collect":
        return lambda: _news_body(deps)
    if kind == "reconcile":
        return lambda: _reconcile_body(deps, p.get("market"))
    if kind == "discover":
        return lambda: _discover_body(deps, p["market"])
    raise ValueError(f"알 수 없는 작업: {kind}")


def _queue_tick(deps: DaemonDeps, heartbeat: Heartbeat | None) -> dict:
    """대기열에서 요청 1건을 꺼내 실행한다. 결과는 `job_requests` 에, runs·하트비트는 run_job 이
    스케줄 잡과 같은 이름(`jobs.job_id_for`)으로 남긴다."""
    if deps.stop_event.is_set():
        return {"skipped": "종료 중"}
    # 잠겨 있으면 claim 하지 않는다(non-blocking). 가벼운 잡도 잠깐 들고 돌아 cron 과 겹치지 않음
    if not deps.heavy_lock.acquire(blocking=False):
        return {"skipped": "다른 무거운 잡 실행 중"}
    try:
        return _queue_tick_locked(deps, heartbeat, kinds=jobq.SLOW_KINDS)
    finally:
        deps.heavy_lock.release()


def _fast_queue_tick(deps: DaemonDeps, heartbeat: Heartbeat | None) -> dict:
    """빠른 레인(전체 검토 P2): 재평가·체결 동기화·뉴스 수집·대조는 heavy 락을 기다리지 않는다.
    백필·scan_analyze 가 돌아도 손절 재평가가 같은 틱에 나간다. 큰 잡은 기존 `job_queue` 만."""
    if deps.stop_event.is_set():
        return {"skipped": "종료 중"}
    return _queue_tick_locked(deps, heartbeat, kinds=jobq.FAST_KINDS)


def _queue_tick_locked(
    deps: DaemonDeps, heartbeat: Heartbeat | None, *, kinds: frozenset[str] | None = None
) -> dict:
    conn = deps.conn_factory()
    try:
        row = jobq.claim_next(conn, now=deps.now(), kinds=kinds)
    finally:
        conn.close()
    if row is None:
        return {"skipped": "대기 없음"}
    req = {**dict(row), "params": json.loads(row["params"])}
    req_id, kind = int(req["id"]), req["kind"]
    name = jobq.job_id_for(kind, req["params"])
    logger.info("대기열 요청 #%d %s 시작 (%s)", req_id, kind, req["source"])

    def _finish(status: str, result: dict | None = None, error: str | None = None) -> None:
        conn = deps.conn_factory()
        try:
            jobq.finish(conn, req_id, status, result=result, error=error, now=deps.now())  # type: ignore[arg-type]
        finally:
            conn.close()

    def wrapped() -> Any:
        try:
            body = queued_job_body(deps, req, _progress_writer(deps, req_id))
            result = body()
        except jobq.JobCancelled as e:
            _finish("skipped", error=str(e))
            return {"skipped": str(e)}
        except Exception as e:
            _finish("error", error=f"{type(e).__name__}: {e}")
            raise
        err = result.get("error") if isinstance(result, dict) else None
        skipped = isinstance(result, dict) and "skipped" in result
        _finish(
            "error" if err else ("skipped" if skipped else "ok"),
            result=result if isinstance(result, dict) else None,
            error=str(err) if err else None,
        )
        return result

    def run_it() -> Any:
        return run_job(
            name, wrapped, conn_factory=deps.conn_factory, interval=False, heartbeat=heartbeat
        )

    run_it()  # heavy 락은 _queue_tick 이 들고 있다(빠른 레인 _fast_queue_tick 은 락 없이)
    return {"request": req_id, "kind": kind}


def _bootstrap_queue(deps: DaemonDeps) -> dict:
    """기동 때: 이전 running → error, 오래된 queued → 만료, 피처 스토어가 비면 백필+학습을 넣는다.
    백필이 24시간 안에 실패했으면 다시 넣지 않는다(재기동 루프 방지)."""
    from trader.ml import features

    now = deps.now()
    conn = deps.conn_factory()
    try:
        stale = jobq.mark_stale_running(conn, now=now)
        expired = jobq.expire_queued(conn, older_than=now - QUEUED_MAX_AGE)
        queued: list[str] = []
        if (
            deps.market_client is not None
            and deps.features_fn is not None
            and not features.has_files(deps.state)
        ):
            last = jobq.latest_by(conn, "features_backfill", source="daemon")
            recent_fail = bool(
                last
                and last["status"] == "error"
                and last.get("finished_at")
                and datetime.fromisoformat(last["finished_at"]) > now - BOOTSTRAP_RETRY_AGE
            )
            if not recent_fail:
                for kind, params in (("features_backfill", {"years": 3}), ("ml_train", {})):
                    try:
                        jobq.enqueue(conn, kind, params, source="daemon", now=now)
                        queued.append(kind)
                    except jobq.DuplicateRequest:
                        pass
    finally:
        conn.close()
    if stale or expired or queued:
        logger.info(
            "대기열 정리: running→error %d, 만료 %d, 부트스트랩 %s", stale, expired, queued or "-"
        )
    return {"stale": stale, "expired": expired, "queued": queued}


# --- 실행 ---------------------------------------------------------------------------


SCHEDULER_POOL_SIZE = 16  # 잡 10개 + heavy 락 대기 몫. 기본 10 이면 잡 수와 같아 여유가 없다


def make_scheduler() -> Any:
    """데몬·트레이 모드가 같은 설정의 BlockingScheduler 를 만든다(풀 크기 명시)."""
    from apscheduler.executors.pool import ThreadPoolExecutor
    from apscheduler.schedulers.blocking import BlockingScheduler

    return BlockingScheduler(
        timezone=KST, executors={"default": ThreadPoolExecutor(SCHEDULER_POOL_SIZE)}
    )


def _start_web(app: Any, host: str, port: int, *, notifier: Any = None) -> threading.Thread:
    """uvicorn 을 데몬 스레드에 띄운다. 포트 점유 등으로 서버가 못 뜨면 스레드만 조용히 끝나므로
    로그와 알림으로 드러낸다(트레이 모드는 stderr 가 보이지 않는다)."""
    import uvicorn

    # 접근 로그는 실시간 시세 폴링(10초)마다 줄이 쌓여 끈다(전체 검토 P10)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="info",
            access_log=False,
            # X-Forwarded-For 로 접속 주소를 바꾸지 않는다(Tailscale 게이트가 실제 피어를 본다)
            proxy_headers=False,
        )
    )

    def run() -> None:
        try:
            server.run()
        except BaseException as e:  # noqa: BLE001 - SystemExit(포트 점유)·소켓 오류 포함
            logger.error("웹 UI 서버 종료 %s:%s: %s", host, port, e)
            if notifier is not None:
                try:
                    notifier.send(
                        messages.event(
                            "웹 UI 시작 실패", f"{host}:{port} — {type(e).__name__}: {e}"
                        )
                    )
                except Exception:  # noqa: BLE001
                    pass
            return
        if not server.started:
            logger.error("웹 UI 서버가 시작되지 못함 %s:%s (포트 점유?)", host, port)
            if notifier is not None:
                try:
                    notifier.send(
                        messages.event(
                            "웹 UI 시작 실패", f"{host}:{port} 포트가 사용 중인지 확인하세요"
                        )
                    )
                except Exception:  # noqa: BLE001
                    pass

    t = threading.Thread(target=run, name="trader-web", daemon=True)
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
        _bootstrap_queue(deps)
        scheduler = make_scheduler() if scheduler_factory is None else scheduler_factory()
        if web and deps.web_app_factory is not None:
            _start_web(
                deps.web_app_factory(),
                deps.settings.web.bind,
                deps.settings.web.port,
                notifier=deps.notifier,
            )
        jobs = build_jobs(deps.schedule, deps.settings, markets=list(deps.settings.markets))
        hb = Heartbeat(base / heartbeat_mod.FILE_NAME)
        hb.start(pid=os.getpid(), started_at=datetime.now(UTC), jobs=jobs)
        _install_heartbeat_listener(scheduler, hb, deps=deps, jobs=jobs)
        for job in jobs:
            scheduler.add_job(
                partial(
                    run_job,
                    job.id,
                    job_body(deps, job, heartbeat=hb),
                    conn_factory=deps.conn_factory,
                    interval=job.interval_s is not None,
                    heartbeat=hb,
                ),
                _trigger(job),
                id=job.id,
                name=job.id,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=misfire_grace_for(job),
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
        if deps.scalp_judge is not None:  # 대기 중 판정 취소(실행 중인 것은 stop_event 로 정리)
            deps.stop_event.set()
            deps.scalp_judge.shutdown()
        _stop_saveticker(deps)
        lock.release()


SCAN_MISFIRE_GRACE_S = 3600  # 절전 등으로 늦어도 1시간 안이면 그냥 돈다(하루를 잃는 것보다 낫다)
DEFAULT_MISFIRE_GRACE_S = 120


def misfire_grace_for(job: Job) -> int:
    return SCAN_MISFIRE_GRACE_S if job.kind == "scan_analyze" else DEFAULT_MISFIRE_GRACE_S


def on_cron_missed(deps: DaemonDeps, job: Job) -> dict:
    """cron 잡을 놓쳤다(grace 초과): 알림 1건 + scan_analyze 는 큐에 따라잡기 1회(중복이면 무시).
    이전엔 하트비트에 missed 만 적혀 Mac 이 07:30 에 잠들어 있으면 그날 분석이 조용히 사라졌다."""
    out: dict[str, Any] = {"job": job.id, "queued": False}
    try:
        deps.notifier.send(
            messages.event("잡 놓침", f"{job.id} — 예정 시각을 지나 실행되지 않았어요")
        )
    except Exception:  # noqa: BLE001
        pass
    if job.kind == "scan_analyze" and job.market:
        conn = deps.conn_factory()
        try:
            jobq.enqueue(
                conn, "scan_analyze", {"market": job.market}, source="daemon", now=deps.now()
            )
            out["queued"] = True
        except jobq.DuplicateRequest:
            pass
        except Exception as e:  # noqa: BLE001
            logger.warning("놓친 scan_analyze 따라잡기 등록 실패 %s: %s", job.id, e)
        finally:
            conn.close()
    return out


def _install_heartbeat_listener(
    scheduler: Any,
    hb: Heartbeat,
    *,
    deps: DaemonDeps | None = None,
    jobs: list[Job] | None = None,
) -> None:
    """APScheduler 이벤트로 잡별 '다음 예정'·놓침을 하트비트에 적는다 (리스너가 없는 가짜는 통과).

    run_job 이 예외를 삼키므로 JOB_ERROR 는 사실상 오지 않고 결과는 run_job 이 적는다. 여기서는
    EXECUTED 뒤 next_run_time 갱신, MISSED·MAX_INSTANCES(앞 실행이 길어 건너뜀) 를 missed 로,
    cron 잡 MISSED 는 `on_cron_missed`(알림·따라잡기)."""
    add = getattr(scheduler, "add_listener", None)
    if add is None:
        return
    try:
        from apscheduler import events as ev
    except ImportError:  # pragma: no cover - 의존성 누락
        return
    by_id = {j.id: j for j in (jobs or [])}

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
            # 대기열 틱은 긴 요청을 도는 동안 10초마다 MAX_INSTANCES 가 나므로 놓침이 아니다
            if not (
                event.code == ev.EVENT_JOB_MAX_INSTANCES
                and event.job_id in (QUEUE_JOB_ID, FAST_QUEUE_JOB_ID)
            ):
                hb.job_missed(event.job_id, datetime.now(UTC))
            job = by_id.get(event.job_id)
            if event.code == ev.EVENT_JOB_MISSED and deps is not None and job and job.cron:
                try:
                    on_cron_missed(deps, job)
                except Exception:  # noqa: BLE001 - 리스너는 죽지 않는다
                    logger.exception("놓친 잡 처리 실패 %s", event.job_id)
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
