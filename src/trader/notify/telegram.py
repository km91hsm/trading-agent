"""텔레그램 발신. 토큰·chat_id 가 없으면 NullNotifier(항상 성공).

토큰이 URL 에 들어가므로 URL·예외 메시지를 로그에 남기지 않는다.
"""

from __future__ import annotations

import html
import logging
from typing import Protocol

import httpx

from trader.config import Secrets

logger = logging.getLogger("trader.notify")

MAX_LEN = 4000


def _quiet_httpx() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class Notifier(Protocol):
    def send(self, text: str, *, escape: bool = True) -> bool: ...


class NullNotifier:
    """설정이 없을 때 쓰는 빈 알림기. 항상 성공으로 돌려준다."""

    def send(self, text: str, *, escape: bool = True) -> bool:
        logger.debug("알림 생략(텔레그램 미설정): %s", text[:80])
        return True


def _chunks(text: str, size: int) -> list[str]:
    out: list[str] = []
    while text:
        if len(text) <= size:
            out.append(text)
            break
        cut = text.rfind("\n", 0, size)
        if cut <= 0:
            cut = size
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, *, http: httpx.Client | None = None) -> None:
        _quiet_httpx()
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._http = http or httpx.Client(timeout=10)

    def send(self, text: str, *, escape: bool = True) -> bool:
        """4,000자 단위로 나눠 보낸다. 기본은 HTML 이스케이프. 실패는 False + 경고 로그."""
        if not text or not text.strip():
            return True
        body = html.escape(text) if escape else text
        for chunk in _chunks(body, MAX_LEN):
            try:
                resp = self._http.post(
                    self._url,
                    data={"chat_id": self._chat_id, "text": chunk, "parse_mode": "HTML"},
                )
            except httpx.HTTPError as e:
                logger.warning("텔레그램 전송 실패: %s", type(e).__name__)
                return False
            if resp.status_code != 200:
                logger.warning("텔레그램 전송 거부: HTTP %s", resp.status_code)
                return False
        return True


def make_notifier(secrets: Secrets, *, http: httpx.Client | None = None) -> Notifier:
    if secrets.TELEGRAM_BOT_TOKEN and secrets.TELEGRAM_CHAT_ID:
        return TelegramNotifier(secrets.TELEGRAM_BOT_TOKEN, secrets.TELEGRAM_CHAT_ID, http=http)
    return NullNotifier()
