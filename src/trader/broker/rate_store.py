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
import time
from collections.abc import Callable
from pathlib import Path

from trader.broker.kis_client import RateLimitBusy, RateLimiter

logger = logging.getLogger("trader.kis")

DB_TIMEOUT_S = 5.0


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

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, timeout=DB_TIMEOUT_S, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS calls(env TEXT NOT NULL, ts REAL NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_env_ts ON calls(env, ts)")
        return conn

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
        if self._disabled:
            super().acquire()
            return
        limit = getattr(self._local, "max_wait", None)
        waited = 0.0
        try:
            conn = self._connect()
        except sqlite3.Error as e:
            self._disabled = True
            logger.warning("공유 레이트리미터 파일을 열 수 없어 프로세스 안 리미터로 동작: %s", e)
            super().acquire()
            return
        try:
            while True:
                try:
                    wait = self._try_take(conn)
                except sqlite3.OperationalError as e:  # 잠금 대기 초과 등: 한 틱 뒤 재시도
                    logger.debug("레이트리미터 잠금 대기: %s", e)
                    wait = 0.05
                if wait is None:
                    return
                if limit is not None and waited + wait > limit:
                    raise RateLimitBusy(limit)
                waited += wait
                self._sleep(wait)
        finally:
            conn.close()
