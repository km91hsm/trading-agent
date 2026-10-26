"""체결통보 리스너(Stock D-060 교훈): 정기 체결 동기화(하루 몇 번)만 기다리면 체결 뒤 잔고·주문
상태가 늦게 바뀐다. 데몬이 계좌 env 마다 KIS 웹소켓에 체결통보(국내 H0STCNI0/9·해외
H0GSCNI0/9, 키 = HTS ID)를 구독하고, 통보 프레임이 오면 **본문은 풀지 않고**(AES — 체결·접수·
정정·취소 무엇이든) 작업 대기열에 `fills_sync` 를 넣는다(중복이면 이미 대기 중). 실제 반영은 기존
`fills.sync_fills` 그대로 — 통보는 '지금 동기화하라' 신호일 뿐이라 원장 규칙(멱등·매칭)은 그대로다.

HTS ID 가 없거나 KIS 가 거절하면 그 env 는 멈추고(다시 붙어도 같다) 정기 동기화만 쓴다. 접속
실패는 지수 백오프, 접속키 거절은 새 키로 다시. 시세 허브(`web/live_quotes`)와 별도 연결이다.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from trader.broker import kis_ws

logger = logging.getLogger("trader.orchestrator")

RECV_TIMEOUT_S = 1.0
MAX_BACKOFF_S = 60.0
DEBOUNCE_S = 5.0  # 통보가 몰리면(분할 체결) 이 간격에 한 번만 동기화를 당긴다


def _default_connect(url: str) -> Any:
    from websockets.sync.client import connect

    return connect(url, open_timeout=10, close_timeout=2, max_size=2**20)


class NoticeListener:
    def __init__(
        self,
        *,
        env: str,
        hts_id: str,
        approval: Callable[[], str],
        invalidate: Callable[[], None],
        on_notice: Callable[[str], None],
        connect: Callable[[str], Any] = _default_connect,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        from trader.broker.tr_ids import WS_URL

        self.env = env
        self._hts = hts_id
        self._approval = approval
        self._invalidate = invalidate
        self._on_notice = on_notice
        self._connect = connect
        self._clock = clock
        self._url = WS_URL["real" if env == "real" else "paper"]
        self._trs = kis_ws.TR_NOTICE["real" if env == "real" else "paper"]
        self._conn: Any = None
        self._attempt = 0
        self._retry_at = 0.0
        self._last_fire = -1e9
        self.rejected: str | None = None
        self.notices = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name=f"kis-notice-{self.env}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5)
        self._close()

    def status(self) -> dict:
        return {
            "env": self.env,
            "connected": self._conn is not None,
            "notices": self.notices,
            "rejected": self.rejected,
        }

    def _run(self) -> None:
        logger.info("체결통보 리스너 시작 (%s)", self.env)
        try:
            while not self._stop.is_set() and self.rejected is None:
                if not self.step():
                    self._stop.wait(1.0)
        except Exception:  # noqa: BLE001 — 리스너가 죽어도 정기 동기화가 있다
            logger.exception("체결통보 리스너 오류 (%s)", self.env)
        finally:
            self._close()
            logger.info("체결통보 리스너 종료 (%s)", self.env)

    def step(self) -> bool:
        """한 바퀴: (필요하면) 접속·구독 → 프레임 하나. 일을 했으면 True."""
        if self.rejected is not None:
            return False
        if self._conn is None and not self._open():
            return False
        try:
            raw = self._conn.recv(timeout=RECV_TIMEOUT_S)
        except TimeoutError:
            return True
        except Exception as e:  # noqa: BLE001 — 끊김: 백오프 뒤 다시
            logger.warning("체결통보 연결 끊김 (%s): %s", self.env, type(e).__name__)
            self._drop(backoff=True)
            return False
        raw = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
        if kis_ws.is_pingpong(raw):
            self._conn.send(raw)
            self._attempt = 0
            return True
        problem = kis_ws.control_problem(raw)
        if problem is not None:
            msg = problem[2].lower()
            if "approval" in msg or msg.strip() == "not found":
                self._invalidate()
                self._drop(backoff=True)
                return False
            if "already" in msg:
                return True
            # HTS ID 거절 등: 다시 붙어도 같다 — 이 env 는 정기 동기화만
            self.rejected = problem[2]
            logger.error(
                "체결통보 구독 거절 (%s): %s — 정기 체결 동기화만 씁니다", self.env, problem[2]
            )
            self._close()
            return False
        self._attempt = 0
        if kis_ws.is_notice(raw):
            self.notices += 1
            now = self._clock()
            if now - self._last_fire >= DEBOUNCE_S:
                self._last_fire = now
                try:
                    self._on_notice(self.env)
                except Exception:  # noqa: BLE001 — 콜백 실패가 연결을 끊지 않는다
                    logger.exception("체결통보 처리 실패 (%s)", self.env)
        return True

    def _open(self) -> bool:
        if self._clock() < self._retry_at:
            return False
        try:
            key = self._approval()
            conn = self._connect(self._url)
            for tr in self._trs:
                conn.send(kis_ws.subscribe_message(key, self._hts, tr_id=tr))
        except Exception as e:  # noqa: BLE001
            logger.warning("체결통보 접속 실패 (%s): %s", self.env, type(e).__name__)
            self._drop(backoff=True)
            return False
        self._conn = conn
        logger.info("체결통보 구독 (%s)", self.env)
        return True

    def _drop(self, *, backoff: bool) -> None:
        self._close()
        if backoff:
            self._attempt += 1
            self._retry_at = self._clock() + min(2 ** (self._attempt - 1), MAX_BACKOFF_S)

    def _close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
