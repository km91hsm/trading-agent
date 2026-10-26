"""KIS HTTP 클라이언트: 공통 헤더, 레이트리밋(토큰 버킷), 오류 통일, EGW00201 재시도."""

from __future__ import annotations

import contextlib
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


class RateLimitBusy(KisApiError):
    """레이트리미터 대기 상한(max_wait) 안에 차례가 오지 않았다 — 웹 호출이 503 으로 바꾼다."""

    def __init__(self, limit: float) -> None:
        super().__init__("BUSY", f"KIS 호출 대기 {limit:.0f}s 초과 (데몬 작업 중)", None)


RATE_PER_SEC: dict[Env, int] = {"real": 20, "paper": 2}
RETRY_CODE = "EGW00201"  # 초당 거래건수 초과 (HTTP 200·500 양쪽으로 온다, 실기 확인)
TOKEN_ERROR_CODES = frozenset({"EGW00123", "EGW00121"})  # 만료·유효하지 않은 토큰
RETRY_WAIT_S = 0.5
MAX_PAGES = 20  # 연속조회 상한(잔고·체결)
RETRY_MAX = 3  # 0.5 → 1.0 → 2.0 초
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
        self._local = threading.local()

    @contextlib.contextmanager
    def max_wait(self, seconds: float | None):
        """이 스레드의 호출이 레이트리미터에서 기다릴 최대 시간. 웹 요청(차트·잔고 새로고침)이
        데몬의 백필·모니터와 같은 리미터를 쓸 때 수초 넘게 매달리지 않도록 503 으로 빠르게
        실패하게 한다. None 이면 무제한(기본)."""
        prev = getattr(self._local, "max_wait", None)
        self._local.max_wait = seconds
        try:
            yield
        finally:
            self._local.max_wait = prev

    def acquire(self) -> None:
        limit = getattr(self._local, "max_wait", None)
        waited = 0.0
        acquired = self._lock.acquire(timeout=limit if limit is not None else -1)
        if not acquired:
            raise RateLimitBusy(limit or 0)
        try:
            while True:
                now = self._clock()
                while self._calls and now - self._calls[0] >= 1.0:
                    self._calls.popleft()
                if len(self._calls) < self._rate:
                    self._calls.append(now)
                    return
                wait = max(1.0 - (now - self._calls[0]), 0.001)
                if limit is not None and waited + wait > limit:
                    raise RateLimitBusy(limit)
                waited += wait
                self._sleep(wait)
        finally:
            self._lock.release()


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
    def limiter(self) -> RateLimiter:
        return self._limiter

    @property
    def base_url(self) -> str:
        return BASE_URL[self.env]

    def _headers(self, tr: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {
            "content-type": "application/json; charset=utf-8",
            **self._auth.headers(),
            "tr_id": tr,
            "custtype": "P",
            **(extra or {}),
        }

    def _once(
        self, method: str, path: str, tr: str, *, extra_headers: dict[str, str] | None = None, **kw
    ) -> tuple[dict, httpx.Headers]:
        """1회 요청. 성공이면 (응답 dict, 응답 헤더), 실패면 KisApiError (KIS 본문의 msg_cd 를
        우선 보존). 헤더는 연속조회(`tr_cont`) 판단에 쓴다."""
        self._limiter.acquire()
        try:
            resp = self._http.request(
                method, self.base_url + path, headers=self._headers(tr, extra_headers), **kw
            )
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
            return data, resp.headers
        if resp.status_code != 200:
            raise KisApiError(f"HTTP{resp.status_code}", resp.text[:200], tr)
        raise KisApiError("INVALID_JSON", "응답이 JSON 객체가 아닙니다", tr)

    def _request(self, method: str, path: str, tr_name: str, **kw: Any) -> dict:
        return self._request_full(method, path, tr_name, **kw)[0]

    def _request_full(
        self, method: str, path: str, tr_name: str, **kw: Any
    ) -> tuple[dict, httpx.Headers]:
        """초당 한도(EGW00201)는 0.5·1.0·2.0초로 최대 RETRY_MAX 회 물러나 재시도하고(백필처럼 연속
        호출에선 한 번으로 모자랐다), 토큰 오류는 캐시를 지우고 1회 재발급 뒤 재시도한다."""
        tr = tr_id(tr_name, self.env)
        wait = RETRY_WAIT_S
        token_retried = False
        for attempt in range(RETRY_MAX + 1):
            try:
                return self._once(method, path, tr, **kw)
            except KisApiError as e:
                if e.msg_cd == RETRY_CODE and attempt < RETRY_MAX:
                    logger.warning("KIS 초당 한도 초과(%s), %.1f초 후 재시도", tr, wait)
                    self._sleep(wait)
                    wait *= 2
                    continue
                if e.msg_cd in TOKEN_ERROR_CODES and not token_retried:
                    logger.warning("KIS 토큰 오류 %s(%s), 캐시 무효화 후 재발급", e.msg_cd, tr)
                    self._auth.invalidate()
                    token_retried = True
                    continue
                raise
        raise AssertionError("unreachable")  # pragma: no cover

    def get(self, path: str, tr_name: str, params: dict[str, Any]) -> dict:
        return self._request("GET", path, tr_name, params=params)

    def get_pages(
        self,
        path: str,
        tr_name: str,
        params: dict[str, Any],
        *,
        ctx_keys: tuple[str, str] = ("CTX_AREA_FK100", "CTX_AREA_NK100"),
        output_key: str = "output1",
        max_pages: int = MAX_PAGES,
    ) -> dict:
        """연속조회. 응답 헤더 `tr_cont` 가 F·M 이면 `tr_cont: N` 과 응답의 ctx_area_* 를 넣어 다음
        페이지를 받고 `output_key` 목록을 이어 붙인다(잔고·체결이 한 페이지(20~50행)를 넘을 때).
        나머지 키(output2 등)는 첫 응답 것. 한 페이지로 끝나면 응답 그대로."""
        fk, nk = ctx_keys
        merged: dict | None = None
        extra: dict[str, str] | None = None
        for _ in range(max_pages):
            data, headers = self._request_full(
                "GET", path, tr_name, params=params, extra_headers=extra
            )
            if merged is None:
                merged = data
            else:
                rows = merged.get(output_key)
                more = data.get(output_key)
                if isinstance(rows, list) and isinstance(more, list):
                    rows.extend(more)
            if str(headers.get("tr_cont", "")).strip() not in ("F", "M"):
                break
            params = {
                **params,
                fk: str(data.get(fk.lower()) or ""),
                nk: str(data.get(nk.lower()) or ""),
            }
            extra = {"tr_cont": "N"}
        else:
            logger.warning("KIS 연속조회 %d페이지 상한 도달 (%s)", max_pages, tr_name)
        return merged or {}

    def post(self, path: str, tr_name: str, body: dict[str, Any]) -> dict:
        """POST(주문·취소). 게이트웨이 5xx 비JSON·rt_cd 없는 본문은 서버가 요청을 처리했을 수 있어
        '실패'가 아니라 응답 없음(UNKNOWN_RESPONSE_CODE)으로 올린다 — executor 가 unknown 으로 남겨
        중복 방지 조건이 살아 있게(전체 검토 O4). KIS 가 본문으로 준 거부(msg_cd)는 그대로."""
        try:
            return self._request("POST", path, tr_name, json=body)
        except KisApiError as e:
            if e.msg_cd == "INVALID_JSON" or e.msg_cd.startswith("HTTP5"):
                raise KisApiError(
                    UNKNOWN_RESPONSE_CODE, f"응답 해석 불가({e.msg_cd}): {e.msg1}", e.tr_id
                ) from None
            raise
