"""KIS 접근 토큰 발급·캐시 (env 별 파일, 만료 1시간 전 갱신, 실패 후 60초 재발급 금지)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from trader.broker.errors import KisApiError
from trader.broker.tr_ids import BASE_URL, TOKEN_PATH
from trader.config import Env
from trader.platform import paths

logger = logging.getLogger("trader.kis")

REFRESH_MARGIN = timedelta(hours=1)
FAIL_BACKOFF = timedelta(seconds=60)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _quiet_httpx() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


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
    ) -> None:
        _quiet_httpx()
        self.env = env
        self._app_key = app_key
        self._app_secret = app_secret
        self._http = http or httpx.Client(timeout=10)
        self._state = state
        self._clock = clock
        self._last_failure: datetime | None = None

    @property
    def cache_path(self) -> Path:
        base = self._state if self._state is not None else paths.state_dir()
        return base / f"kis_token_{self.env}.json"

    def _read_cache(self) -> str | None:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            token = data["access_token"]
            expires_at = datetime.fromisoformat(data["expires_at"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if self._clock() >= expires_at - REFRESH_MARGIN:
            return None
        return token

    def _write_cache(self, token: str, expires_at: datetime) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"access_token": token, "expires_at": expires_at.isoformat()}),
            encoding="utf-8",
        )
        tmp.replace(self.cache_path)

    def _issue(self) -> str:
        now = self._clock()
        if self._last_failure is not None and now - self._last_failure < FAIL_BACKOFF:
            raise KisApiError("AUTH", "토큰 발급 실패 직후라 60초 동안 재발급하지 않습니다")
        body = {
            "grant_type": "client_credentials",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
        }
        try:
            resp = self._http.post(BASE_URL[self.env] + TOKEN_PATH, json=body)
        except httpx.HTTPError as e:
            self._last_failure = now
            raise KisApiError("NETWORK", f"토큰 발급 요청 실패: {type(e).__name__}") from None
        if resp.status_code != 200:
            self._last_failure = now
            raise KisApiError(f"HTTP{resp.status_code}", "토큰 발급 거부") from None
        try:
            data = resp.json()
            token = str(data["access_token"])
            seconds = int(data.get("expires_in", 86400))
        except (ValueError, KeyError, TypeError):
            self._last_failure = now
            raise KisApiError("INVALID_JSON", "토큰 응답을 해석할 수 없습니다") from None
        self._last_failure = None
        self._write_cache(token, now + timedelta(seconds=seconds))
        logger.info("KIS 토큰 발급 (%s)", self.env)
        return token

    def token(self) -> str:
        cached = self._read_cache()
        if cached is not None:
            return cached
        return self._issue()

    def headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self.token()}",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
        }
