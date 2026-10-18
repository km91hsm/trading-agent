"""KIS HTTP 클라이언트: 공통 헤더, 레이트리밋(토큰 버킷), 오류 통일, EGW00201 재시도."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import httpx

from trader.broker.errors import UNKNOWN_RESPONSE_CODE, KisApiError
from trader.broker.kis_auth import KisAuth
from trader.broker.tr_ids import BASE_URL, tr_id
from trader.config import Env

logger = logging.getLogger("trader.kis")

RATE_PER_SEC: dict[Env, int] = {"real": 20, "paper": 2}
RETRY_CODE = "EGW00201"  # 초당 거래건수 초과 (HTTP 200·500 양쪽으로 온다, 실기 확인)
TOKEN_ERROR_CODES = frozenset({"EGW00123", "EGW00121"})  # 만료·유효하지 않은 토큰
RETRY_WAIT_S = 0.5
# 요청이 서버에 닿은 뒤 응답을 못 받은 실패 → UNKNOWN_RESPONSE_CODE (ConnectError 는 NETWORK)
_SENT_BUT_NO_RESPONSE = (
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)


class RateLimiter:
    """1초 슬라이딩 윈도 안의 호출 수를 rate_per_s 이하로 제한한다."""

    def __init__(
        self,
        rate_per_s: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._rate = rate_per_s
        self._clock = clock
        self._sleep = sleep
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            while True:
                now = self._clock()
                while self._calls and now - self._calls[0] >= 1.0:
                    self._calls.popleft()
                if len(self._calls) < self._rate:
                    self._calls.append(now)
                    return
                self._sleep(max(1.0 - (now - self._calls[0]), 0.001))


class KisClient:
    def __init__(
        self,
        env: Env,
        auth: KisAuth,
        *,
        http: httpx.Client | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        if auth.env != env:
            raise ValueError(f"인증 env({auth.env})와 클라이언트 env({env})가 다릅니다")
        self.env = env
        self._auth = auth
        self._http = http or httpx.Client(timeout=10)
        self._limiter = limiter or RateLimiter(RATE_PER_SEC[env])
        self._sleep: Callable[[float], None] = time.sleep

    @property
    def base_url(self) -> str:
        return BASE_URL[self.env]

    def _headers(self, tr: str) -> dict[str, str]:
        return {
            "content-type": "application/json; charset=utf-8",
            **self._auth.headers(),
            "tr_id": tr,
            "custtype": "P",
        }

    def _once(self, method: str, path: str, tr: str, **kw: Any) -> dict:
        """1회 요청. 성공이면 응답 dict, 실패면 KisApiError (KIS 본문의 msg_cd 를 우선 보존)."""
        self._limiter.acquire()
        try:
            resp = self._http.request(method, self.base_url + path, headers=self._headers(tr), **kw)
        except _SENT_BUT_NO_RESPONSE as e:
            raise KisApiError(UNKNOWN_RESPONSE_CODE, f"응답 없음: {type(e).__name__}", tr) from None
        except httpx.HTTPError as e:
            raise KisApiError("NETWORK", f"요청 실패: {type(e).__name__}", tr) from None
        try:
            data = resp.json()
        except ValueError:
            data = None
        if isinstance(data, dict):
            # KIS 는 오류를 HTTP 200·500 양쪽으로 주며 msg_cd/error_code 가 진짜 원인이다.
            code = str(data.get("msg_cd") or data.get("error_code") or "")
            msg = str(data.get("msg1") or data.get("error_description") or "").strip()
            if code and (resp.status_code != 200 or str(data.get("rt_cd", "1")) != "0"):
                raise KisApiError(code, msg, tr)
            if "rt_cd" not in data:
                raise KisApiError("INVALID_JSON", "rt_cd 가 없는 응답입니다", tr)
            if str(data["rt_cd"]) != "0":
                raise KisApiError("UNKNOWN", msg, tr)
            return data
        if resp.status_code != 200:
            raise KisApiError(f"HTTP{resp.status_code}", resp.text[:200], tr)
        raise KisApiError("INVALID_JSON", "응답이 JSON 객체가 아닙니다", tr)

    def _request(self, method: str, path: str, tr_name: str, **kw: Any) -> dict:
        tr = tr_id(tr_name, self.env)
        try:
            return self._once(method, path, tr, **kw)
        except KisApiError as e:
            if e.msg_cd == RETRY_CODE:
                logger.warning("KIS 초당 한도 초과(%s), %.1f초 후 재시도", tr, RETRY_WAIT_S)
                self._sleep(RETRY_WAIT_S)
            elif e.msg_cd in TOKEN_ERROR_CODES:
                logger.warning("KIS 토큰 오류 %s(%s), 캐시 무효화 후 재발급", e.msg_cd, tr)
                self._auth.invalidate()
            else:
                raise
        return self._once(method, path, tr, **kw)

    def get(self, path: str, tr_name: str, params: dict[str, Any]) -> dict:
        return self._request("GET", path, tr_name, params=params)

    def post(self, path: str, tr_name: str, body: dict[str, Any]) -> dict:
        return self._request("POST", path, tr_name, json=body)
