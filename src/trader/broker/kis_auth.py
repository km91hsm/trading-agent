"""KIS 접근 토큰 발급·캐시.

- 앱키 묶음별 파일 `state/kis_token_<name>.json` = {access_token, expires_at, failed_at}
  (name 은 공용 키면 env, 계좌 전용 키면 `<env>-<접두사>` — 키마다 토큰이 따로라 섞으면 덮어쓴다)
- 만료 1시간 전부터 갱신을 시도하되, 갱신이 실패해도 아직 유효한 토큰이 있으면 그것을 쓴다.
- 발급 실패 시각(failed_at)을 파일에 기록해 다른 인스턴스·프로세스도 60초 백오프를 지킨다.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from trader.broker.errors import KisApiError
from trader.broker.tr_ids import APPROVAL_PATH, BASE_URL, TOKEN_PATH
from trader.config import Env
from trader.logging_setup import quiet_httpx
from trader.platform import paths
from trader.platform.fsutil import atomic_replace

logger = logging.getLogger("trader.kis")

REFRESH_MARGIN = timedelta(hours=1)
FAIL_BACKOFF = timedelta(seconds=60)
APPROVAL_TTL = timedelta(hours=20)  # 접속키 유효 24시간 — 여유를 두고 새로


def _utc_now() -> datetime:
    return datetime.now(UTC)


class KisAuth:
    def __init__(
        self,
        env: Env,
        app_key: str,
        app_secret: str,
        *,
        http: httpx.Client | None = None,
        state: Path | None = None,
        clock: Callable[[], datetime] = _utc_now,
        name: str | None = None,
    ) -> None:
        quiet_httpx()
        self.env = env
        self.name = name or env
        from trader.config import key_fingerprint

        self._fp = key_fingerprint(app_key) if app_key else ""
        self._app_key = app_key
        self._app_secret = app_secret
        self._http = http or httpx.Client(timeout=10)
        self._state = state
        self._clock = clock
        self._mem: tuple[str, datetime] | None = None  # (token, expires_at) 프로세스 메모
        self._approval: tuple[str, datetime] | None = None  # 웹소켓 접속키 (프로세스 메모)
        self._approval_lock = threading.Lock()

    @property
    def cache_path(self) -> Path:
        base = self._state if self._state is not None else paths.state_dir()
        return base / f"kis_token_{self.name}.json"

    # --- 캐시 ---------------------------------------------------------------

    def _read_cache(self) -> dict:
        """{token, expires_at, failed_at} — 없거나 깨졌으면 빈 dict."""
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict = {}
        if data.get("key") and data.get("key") != self._fp:
            return {}  # 다른 앱키로 받은 토큰(키를 바꿨다) — 쓰지 않고 새로 받는다
        try:
            if data.get("access_token") and data.get("expires_at"):
                out["token"] = str(data["access_token"])
                out["expires_at"] = datetime.fromisoformat(str(data["expires_at"]))
            if data.get("failed_at"):
                out["failed_at"] = datetime.fromisoformat(str(data["failed_at"]))
        except (ValueError, TypeError):
            return {}
        return out

    def _write_cache(self, payload: dict) -> None:
        """pid 접미사 임시파일 후 replace. 쓰기 실패는 로그만 남긴다(토큰은 이미 손에 있다)."""
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_name(f"{self.cache_path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            atomic_replace(tmp, self.cache_path)
        except OSError as e:
            logger.warning("토큰 캐시 쓰기 실패(%s): %s", self.env, type(e).__name__)

    def _cache_payload(self, cache: dict, **updates: object) -> dict:
        payload: dict = {"key": self._fp}
        if cache.get("token"):
            payload["access_token"] = cache["token"]
            payload["expires_at"] = cache["expires_at"].isoformat()
        if cache.get("failed_at"):
            payload["failed_at"] = cache["failed_at"].isoformat()
        payload.update(updates)
        return payload

    def invalidate(self) -> None:
        """토큰이 서버에서 무효화됐을 때 캐시를 버린다."""
        self._mem = None
        try:
            self.cache_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("토큰 캐시 삭제 실패(%s): %s", self.env, type(e).__name__)

    # --- 발급 ---------------------------------------------------------------

    def _issue(self, cache: dict) -> str:
        now = self._clock()
        failed_at = cache.get("failed_at")
        if failed_at is not None and now - failed_at < FAIL_BACKOFF:
            raise KisApiError("AUTH", "토큰 발급 실패 직후라 60초 동안 재발급하지 않습니다")
        body = {
            "grant_type": "client_credentials",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
        }
        try:
            resp = self._http.post(BASE_URL[self.env] + TOKEN_PATH, json=body)
        except httpx.HTTPError as e:
            self._mark_failure(cache, now)
            raise KisApiError("NETWORK", f"토큰 발급 요청 실패: {type(e).__name__}") from None
        if resp.status_code != 200:
            self._mark_failure(cache, now)
            raise KisApiError(f"HTTP{resp.status_code}", "토큰 발급 거부") from None
        try:
            data = resp.json()
            token = str(data["access_token"])
            seconds = int(data.get("expires_in", 86400))
        except (ValueError, KeyError, TypeError):
            self._mark_failure(cache, now)
            raise KisApiError("INVALID_JSON", "토큰 응답을 해석할 수 없습니다") from None
        expires_at = now + timedelta(seconds=seconds)
        self._write_cache(
            {"access_token": token, "expires_at": expires_at.isoformat(), "key": self._fp}
        )
        logger.info("KIS 토큰 발급 (%s)", self.env)
        return token

    def _mark_failure(self, cache: dict, now: datetime) -> None:
        # 발급 전 스냅샷(cache)이 아니라 **지금 파일에 있는 것**에 failed_at 만 덧붙인다.
        # 다른 프로세스가 같은 갱신 창에서 막 발급한 새 토큰을 옛 스냅샷으로 덮어쓰지 않도록.
        del cache
        self._write_cache(self._cache_payload(self._read_cache(), failed_at=now.isoformat()))

    def token(self) -> str:
        now = self._clock()
        if self._mem is not None and now < self._mem[1] - REFRESH_MARGIN:
            return self._mem[0]  # 호출마다 캐시 파일을 읽지 않는다 (백필 수천 호출)
        tok = self._token_from_cache_or_issue(now)
        return tok

    def _token_from_cache_or_issue(self, now: datetime) -> str:
        cache = self._read_cache()
        token = cache.get("token")
        expires_at = cache.get("expires_at")
        if token and expires_at and now < expires_at - REFRESH_MARGIN:
            self._mem = (token, expires_at)
            return token
        try:
            issued = self._issue(cache)
            fresh = self._read_cache()
            if fresh.get("token") == issued and fresh.get("expires_at"):
                self._mem = (issued, fresh["expires_at"])
            return issued
        except KisApiError as e:
            # 갱신 창 안의 실패: 아직 유효한 토큰이 있으면 그대로 쓴다.
            if token and expires_at and now < expires_at:
                logger.warning("토큰 갱신 실패(%s), 기존 토큰 사용: %s", self.env, e.msg_cd)
                return token
            raise

    # --- 웹소켓 접속키 -------------------------------------------------------

    def approval_key(self) -> str:
        """실시간 웹소켓 접속키(토큰과 별개, 프로세스 메모 `APPROVAL_TTL`). 여러 스레드가 동시에
        요구해도 한 번만 발급한다 — 같은 앱키로 새 키를 받으면 옛 키는 거절된다."""
        with self._approval_lock:
            now = self._clock()
            if self._approval is not None and now < self._approval[1]:
                return self._approval[0]
            body = {
                "grant_type": "client_credentials",
                "appkey": self._app_key,
                "secretkey": self._app_secret,
            }
            try:
                resp = self._http.post(BASE_URL[self.env] + APPROVAL_PATH, json=body)
            except httpx.HTTPError as e:
                raise KisApiError("NETWORK", f"접속키 발급 요청 실패: {type(e).__name__}") from None
            if resp.status_code != 200:
                raise KisApiError(f"HTTP{resp.status_code}", "접속키 발급 거부") from None
            try:
                key = str(resp.json()["approval_key"])
            except (ValueError, KeyError, TypeError):
                raise KisApiError("INVALID_JSON", "접속키 응답을 해석할 수 없습니다") from None
            if not key:
                raise KisApiError("INVALID_JSON", "접속키 응답이 비어 있습니다")
            self._approval = (key, now + APPROVAL_TTL)
            logger.info("KIS 웹소켓 접속키 발급 (%s)", self.env)
            return key

    def invalidate_approval(self) -> None:
        """서버가 접속키를 거절했을 때(새 키를 받은 다른 연결 등) 버린다."""
        with self._approval_lock:
            self._approval = None

    def headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self.token()}",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
        }
