"""화면 실시간 시세 허브 — 국내 체결가를 KIS 웹소켓 하나로 받아 `/api/quotes` 가 바로 돌려준다.

- **임차**: `/api/quotes?market=KR` 요청이 그 종목들을 `LEASE_S` 동안 빌린다. 허브는 최근 요청
  순으로 `MAX_TICKERS`(41칸 중 40) 종목만 구독하고, 임차가 끝난 종목은 해제한다 — 탭을 닫으면 칸이
  돌아온다.
  브라우저 쪽은 지금처럼 보이는 탭이 폴링(웹소켓이 붙어 있으면 1초)하므로 탭마다 연결이 늘지 않는다.
- **스레드 하나**: 첫 요청에 뜨고, 정규장이 아니거나 `IDLE_S` 동안 요청이 없으면 연결을 닫고 끝난다.
  연결은 이 스레드만 만진다(요청 스레드는 임차·캐시만 본다).
- **장애**: 접속 실패는 지수 백오프, 접속키 거절은 키를 버리고 재접속, 거절된 종목은 다시 청하지
  않는다, 틱이 없는 종목은 결과에서 빠져 호출부가 REST 로 채운다.
- **표시 전용**: 신호·주문·게이트는 이 값을 읽지 않는다.
- **앱키 세션 양보**: KIS 는 앱키당 웹소켓 세션 하나라, 같은 프로세스의 실전 체결통보 리스너가
  세션을 쓰는 동안(`blocked()`)은 붙지 않고 화면은 REST 로 돈다. 다른 프로세스가 쥐고 있으면
  ('ALREADY IN USE appkey') 백오프한다.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from trader.broker import kis_ws

logger = logging.getLogger("trader.web")

Sub = tuple[str, str]  # (TR, 키)

LEASE_S = 30.0  # 마지막 요청 뒤 이만큼 구독을 유지
IDLE_S = 60.0  # 아무 요청도 없으면 연결을 닫고 스레드를 끝낸다
# 41칸 = 지수 2(KOSPI·KOSDAQ) + 호가 1 + 체결가 37 + 여유 1
MAX_TICKERS = kis_ws.MAX_SUBSCRIPTIONS - 1 - len(kis_ws.INDEX_CODES) - 1
BOOK_LEASE_S = 10.0  # 호가는 차트 화면이 보는 동안만
RECV_TIMEOUT_S = 0.5
MAX_BACKOFF_S = 30.0
TICK_FRESH_S = 10.0  # 이보다 오래된 틱(체결이 멈췄거나 끊김을 아직 모름)은 REST 로 보완
REJECT_TTL_S = 600.0  # 구독 거절 종목은 이 동안 다시 청하지 않는다(일시 거절이면 그 뒤 다시)


def _default_connect(url: str) -> Any:
    from websockets.sync.client import connect

    return connect(url, open_timeout=10, close_timeout=2, max_size=2**20)


def _kr_open(now: datetime) -> bool:
    from trader import market_hours

    return market_hours.is_open("KR", now)


class QuoteHub:
    def __init__(
        self,
        *,
        approval: Callable[[], str],
        invalidate: Callable[[], None],
        connect: Callable[[str], Any] = _default_connect,
        url: str | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        clock: Callable[[], float] = time.monotonic,
        is_open: Callable[[datetime], bool] = _kr_open,
        autostart: bool = True,
        blocked: Callable[[], bool] = lambda: False,
    ) -> None:
        from trader.broker.tr_ids import WS_URL

        self._approval = approval
        self._invalidate = invalidate
        self._connect = connect
        self._url = url or WS_URL["real"]
        self._now = now
        self._clock = clock
        self._is_open = is_open
        self._autostart = autostart
        self.blocked = blocked
        self._lock = threading.Lock()
        # 구독 키는 (TR, 키): 체결가 (H0STCNT0, 종목)·지수 (H0UPCNT0, 0001)·호가 (H0STASP0, 종목)
        self._leases: dict[Sub, float] = {}  # 키 → 마지막 요청 시각(clock)
        self._ticks: dict[Sub, tuple[float, dict]] = {}  # 키 → (받은 시각 clock, 값)
        self._subs: set[Sub] = set()  # 지금 구독 중(허브 스레드만 바꾼다; 읽기는 락 안에서)
        self._rejected: dict[Sub, float] = {}  # 키 → 거절 시각(clock)
        self._conn: Any = None
        self._key = ""
        self._attempt = 0
        self._retry_at = 0.0
        self._last_request = 0.0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @classmethod
    def for_market(
        cls, market: Any, *, blocked: Callable[[], bool] | None = None
    ) -> QuoteHub | None:
        """실전 시세 클라이언트(`KisMarket`)의 접속키로 만든다. 없으면 None(폴링만)."""
        if market is None or not hasattr(market, "approval_key"):
            return None
        return cls(
            approval=market.approval_key,
            invalidate=market.invalidate_approval,
            blocked=blocked or (lambda: False),
        )

    # --- 요청 스레드 쪽 ----------------------------------------------------

    def _lease(self, keys: list[Sub]) -> dict[Sub, dict]:
        """키들을 빌리고, 구독 중이며 신선한 값이 있는 것만 돌려준다."""
        if not self._is_open(self._now()) or self.blocked():
            return {}
        now = self._clock()
        with self._lock:
            self._last_request = now
            for k in keys:
                self._leases[k] = now
            out = {
                k: dict(self._ticks[k][1])
                for k in keys
                if k in self._subs and k in self._ticks and now - self._ticks[k][0] <= TICK_FRESH_S
            }
        if self._autostart:
            self._ensure_thread()
        return out

    def request(self, tickers: list[str]) -> dict[str, dict]:
        """이 종목들을 빌리고, 구독 중이며 틱이 있는 것만 돌려준다(나머지는 호출부가 REST)."""
        got = self._lease([(kis_ws.TR_TRADE, t) for t in tickers])
        return {k[1]: v for k, v in got.items()}

    def request_indices(self) -> dict[str, dict]:
        """KOSPI·KOSDAQ 실시간 지수(상단 바). {"KOSPI": {price, change_pct, asof}}."""
        got = self._lease([(kis_ws.TR_INDEX, c) for c in kis_ws.INDEX_CODES])
        return {kis_ws.INDEX_CODES[k[1]]: v for k, v in got.items()}

    def request_book(self, ticker: str) -> dict | None:
        """호가 10단(차트 화면). 지수 지정가 보조 — 표시 전용."""
        return self._lease([(kis_ws.TR_BOOK, ticker)]).get((kis_ws.TR_BOOK, ticker))

    def status(self) -> dict:
        with self._lock:
            return {"connected": self._conn is not None, "subscribed": len(self._subs)}

    def stop(self) -> None:
        """웹 앱 종료 때. 연결은 그것을 연 허브 스레드가 닫는다(종료 신호를 보고 finally 에서)."""
        self._stop.set()
        with self._lock:
            t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5)
        if t is None:
            self._disconnect()

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._stop.is_set() or (self._thread is not None and self._thread.is_alive()):
                return
            self._thread = threading.Thread(target=self._run, name="kis-ws-quotes", daemon=True)
            self._thread.start()

    # --- 허브 스레드 ------------------------------------------------------

    def _run(self) -> None:
        logger.info("실시간 시세 허브 시작")
        decided = self._clock()
        try:
            while not self._stop.is_set():
                with self._lock:
                    decided = self._clock()
                    idle = decided - self._last_request > IDLE_S
                if idle or not self._is_open(self._now()) or self.blocked():
                    break
                if not self.step():
                    self._stop.wait(RECV_TIMEOUT_S)
        except Exception:  # noqa: BLE001 — 허브가 죽어도 화면은 REST 로 계속 돈다
            logger.exception("실시간 시세 허브 오류")
        finally:
            self._disconnect()
            with self._lock:
                self._thread = None
                # 끝내기로 정한 뒤 들어온 요청이 있으면 이어받는다(그 요청은 스레드가 살아 있어
                # 새로 띄우지 않았다)
                handoff = self._last_request > decided and not self._stop.is_set()
            logger.info("실시간 시세 허브 종료")
            if handoff and self._autostart:
                self._ensure_thread()

    def _desired(self) -> list[Sub]:
        now = self._clock()

        def ttl(k: Sub) -> float:
            return BOOK_LEASE_S if k[0] == kis_ws.TR_BOOK else LEASE_S

        with self._lock:
            for k in [k for k, at in self._rejected.items() if now - at > REJECT_TTL_S]:
                del self._rejected[k]
            for k in [k for k, at in self._leases.items() if now - at > ttl(k)]:
                del self._leases[k]
            live = sorted(
                ((at, k) for k, at in self._leases.items() if k not in self._rejected),
                reverse=True,  # 최근 요청부터
            )
        index = [k for _, k in live if k[0] == kis_ws.TR_INDEX]
        book = [k for _, k in live if k[0] == kis_ws.TR_BOOK][:1]
        trade = [k for _, k in live if k[0] == kis_ws.TR_TRADE][:MAX_TICKERS]
        return index + book + trade

    def step(self) -> bool:
        """한 바퀴: (필요하면) 접속 → 구독 맞추기 → 프레임 하나 받기. 일을 했으면 True."""
        if not self._is_open(self._now()):
            self._disconnect()
            return False
        if self._conn is None and not self._open_connection():
            return False
        desired = set(self._desired())
        try:
            self._sync(desired)
            raw = self._conn.recv(timeout=RECV_TIMEOUT_S)
        except TimeoutError:
            return True
        except Exception as e:  # noqa: BLE001 — 끊김: 백오프 뒤 다시
            logger.warning("실시간 시세 연결 끊김: %s", type(e).__name__)
            self._drop(backoff=True)
            return False
        # 정상 응답을 받은 뒤에야 백오프를 푼다(접속 직후 끊김·접속키 거절 반복이면 간격이 는다)
        if self._handle(raw if isinstance(raw, str) else raw.decode("utf-8", "replace")):
            self._attempt = 0
        return True

    def _open_connection(self) -> bool:
        if self._clock() < self._retry_at:
            return False
        try:
            key = self._approval()
            conn = self._connect(self._url)
        except Exception as e:  # noqa: BLE001 — 접속키·연결 실패는 백오프
            logger.warning("실시간 시세 접속 실패: %s", type(e).__name__)
            self._drop(backoff=True)
            return False
        if self._stop.is_set():  # 접속하는 동안 웹 앱이 내려갔다
            conn.close()
            return False
        with self._lock:
            self._conn, self._key, self._subs = conn, key, set()
        logger.info("실시간 시세 접속")
        return True

    def _sync(self, desired: set[Sub]) -> None:
        with self._lock:
            current = set(self._subs)
        for k in sorted(current - desired):
            self._conn.send(kis_ws.subscribe_message(self._key, k[1], release=True, tr_id=k[0]))
            with self._lock:
                self._subs.discard(k)
                self._ticks.pop(k, None)
        for k in sorted(desired - current):
            self._conn.send(kis_ws.subscribe_message(self._key, k[1], tr_id=k[0]))
            with self._lock:
                self._subs.add(k)

    def _handle(self, raw: str) -> bool:
        """정상 응답이면 True(백오프 해제), 접속키·해제 거절로 연결을 버렸으면 False."""
        if kis_ws.is_pingpong(raw):
            self._conn.send(raw)  # 돌려주지 않으면 서버가 끊는다
            return True
        problem = kis_ws.control_problem(raw)
        if problem is not None:
            tr, tr_key, msg = problem
            key: Sub | None = (tr, tr_key) if tr_key else None
            low = msg.lower()
            if "approval" in low or low.strip() == "not found":
                logger.warning("실시간 접속키 거절 — 새 키로 다시 접속합니다")
                self._invalidate()
                self._drop(backoff=True)  # 거절이 반복되면 키 재발급도 간격을 늘린다
                return False
            if kis_ws.is_appkey_in_use(msg):
                logger.warning("실시간 시세: 앱키 세션 사용 중(다른 연결) — 잠시 뒤 다시")
                self._drop(backoff=True)
                return False
            if "already" in low or not key:
                return True
            with self._lock:
                registered = key in self._subs
            if not registered:
                # 해제가 거절됐다 — 서버에는 구독이 남았을 수 있어 로컬과 어긋난다. 다시 접속해 새로
                # 맞춘다(41칸 한도를 넘기지 않게)
                logger.info("실시간 구독 해제 거절 %s: %s — 다시 접속합니다", key, msg)
                self._drop(backoff=True)
                return False
            logger.info("실시간 구독 거절 %s: %s", key, msg)
            with self._lock:
                self._rejected[key] = self._clock()
                self._subs.discard(key)
                self._ticks.pop(key, None)
            return True
        stamp, at = self._now().isoformat(), self._clock()
        for rec in kis_ws.parse_frame(raw):
            tr = rec["tr_id"]
            key = (tr, rec["code"] if tr == kis_ws.TR_INDEX else rec["ticker"])
            if tr == kis_ws.TR_BOOK:
                val = {k: rec[k] for k in ("asks", "bids", "total_ask", "total_bid", "time")}
            else:
                val = {"price": rec["price"], "change_pct": rec["change_pct"], "stale": False}
                val["source"] = "ws"
            val["asof"] = stamp
            with self._lock:
                if key in self._subs:
                    self._ticks[key] = (at, val)
        return True

    def _drop(self, *, backoff: bool) -> None:
        self._disconnect()
        if backoff:
            self._attempt += 1
            self._retry_at = self._clock() + min(2 ** (self._attempt - 1), MAX_BACKOFF_S)
        else:
            self._retry_at = 0.0

    def _disconnect(self) -> None:
        with self._lock:
            conn, self._conn = self._conn, None
            self._subs = set()
            self._ticks = {}
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
