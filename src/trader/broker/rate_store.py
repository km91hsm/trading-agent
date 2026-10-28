"""교차 프로세스 KIS 레이트리미터 (리뷰 F10).

`RateLimiter` 는 프로세스 안에서만 호출 수를 세는데, 오케스트레이터 세션은 도구 호출마다
`trader tool` 서브프로세스를 새로 띄우므로 데몬(monitor·백필)과 세션 도구가 각자 20/s(실전)·
2/s(모의) 를 쓰면 합이 한도를 넘어 EGW00201 이 난다. 호출 시각을 작은 SQLite 파일
(`state/kis_rate.db`, 원장과 분리해 잠금을 섞지 않음)에 env 별로 적고 `BEGIN IMMEDIATE` 로 세어
프로세스가 몇 개든 1초 슬라이딩 윈도 안의 합을 rate 이하로 막는다. 시계는 `time.time`(프로세스
간 공통)이다. 파일을 열 수 없으면 프로세스 안 리미터로만 동작한다(경고 1회).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

from trader.broker.kis_client import RateLimitBusy, RateLimiter

logger = logging.getLogger("trader.kis")

DB_TIMEOUT_S = 5.0
RETRY_S = 300.0  # 파일을 못 열어 프로세스 안 리미터로 내려간 뒤 다시 열어 볼 간격(2-P8)

# 교차 프로세스 제한이 꺼진 리미터(경로|env → 상태). 같은 프로세스의 운영 점검이 읽는다(2-P8) —
# 꺼지면 데몬과 `trader tool` 이 같은 1초 창을 나누지 못해 EGW00201 이 다시 날 수 있다.
_DISABLED: dict[str, dict] = {}
_DISABLED_LOCK = threading.Lock()


def disabled_status() -> list[dict]:
    """이 프로세스에서 교차 프로세스 제한이 꺼진 리미터 목록 [{env, path, since, error}]."""
    with _DISABLED_LOCK:
        return [dict(v) for v in _DISABLED.values()]


class SharedRateLimiter(RateLimiter):
    """파일(SQLite) 기반으로 여러 프로세스가 같은 1초 창을 나눠 쓰는 리미터."""

    def __init__(
        self,
        rate_per_s: int,
        path: Path,
        env: str,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(rate_per_s, clock=clock, sleep=sleep)
        self._path = Path(path)
        self._env = env
        self._disabled = False
        self._disabled_at = 0.0
        # 스레드마다 연결 하나를 재사용한다(전체 검토 P13): KIS 호출마다 연결·PRAGMA·DDL 을 다시
        # 하면 호출당 수 ms 와 fsync 가 더 든다. sqlite3 연결은 만든 스레드에서만 쓴다
        self._conns = threading.local()
        self._ddl_done = False
        self._ddl_lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._conns, "conn", None)
        if conn is not None:
            return conn
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, timeout=DB_TIMEOUT_S, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            with self._ddl_lock:
                if not self._ddl_done:
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS calls(env TEXT NOT NULL, ts REAL NOT NULL)"
                    )
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_env_ts ON calls(env, ts)")
                    self._ddl_done = True
        except sqlite3.Error:
            conn.close()  # 초기화 중 실패한 연결을 남기지 않는다(2-P8)
            raise
        # 스레드가 끝나면 threading.local 값이 지워지며 연결도 해제된다(CPython 참조 해제 시 close)
        self._conns.conn = conn
        return conn

    def _key(self) -> str:
        return f"{self._path}|{self._env}"

    def _disable(self, e: Exception) -> None:
        self._disabled = True
        self._disabled_at = self._clock()
        with _DISABLED_LOCK:
            _DISABLED[self._key()] = {
                "env": self._env,
                "path": str(self._path),
                "since": self._disabled_at,
                "error": f"{type(e).__name__}: {e}",
            }
        logger.warning(
            "공유 레이트리미터 파일을 열 수 없어 프로세스 안 리미터로 동작(%.0f초 뒤 재시도): %s",
            RETRY_S,
            e,
        )

    def _enable(self) -> None:
        self._disabled = False
        with _DISABLED_LOCK:
            _DISABLED.pop(self._key(), None)
        logger.info("공유 레이트리미터 다시 사용: %s", self._env)

    def _drop_conn(self) -> None:
        conn = getattr(self._conns, "conn", None)
        self._conns.conn = None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def _try_take(self, conn: sqlite3.Connection) -> float | None:
        """자리를 잡으면 None, 아니면 기다릴 초."""
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = self._clock()
            conn.execute("DELETE FROM calls WHERE env=? AND ts <= ?", (self._env, now - 1.0))
            row = conn.execute(
                "SELECT COUNT(*) AS n, MIN(ts) AS oldest FROM calls WHERE env=?", (self._env,)
            ).fetchone()
            if int(row[0]) < self._rate:
                conn.execute("INSERT INTO calls(env, ts) VALUES (?, ?)", (self._env, now))
                return None
            return max(1.0 - (now - float(row[1])), 0.001)
        finally:
            conn.execute("COMMIT")

    def acquire(self) -> None:
        retrying = False
        if self._disabled:
            if self._clock() - self._disabled_at < RETRY_S:
                super().acquire()
                return
            retrying = True  # 간격이 지났다 — 파일을 다시 열어 본다
        limit = getattr(self._local, "max_wait", None)
        waited = 0.0
        try:
            conn = self._connect()
        except (sqlite3.Error, OSError) as e:
            self._disable(e)
            super().acquire()
            return
        if retrying:
            self._enable()
        while True:
            try:
                wait = self._try_take(conn)
            except sqlite3.OperationalError as e:  # 잠금 대기 초과 등: 한 틱 뒤 재시도
                logger.debug("레이트리미터 잠금 대기: %s", e)
                if conn.in_transaction:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        self._drop_conn()
                        conn = self._connect()
                wait = 0.05
            if wait is None:
                return
            if limit is not None and waited + wait > limit:
                raise RateLimitBusy(limit)
            waited += wait
            self._sleep(wait)
