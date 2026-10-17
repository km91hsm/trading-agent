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


def _chunks(text: str, size: int, *, escape: bool) -> list[str]:
    """원문을 잘라 청크마다 이스케이프한다. 이스케이프 후 길이가 size 를 넘지 않게 하고,
    엔티티(&lt; 등)는 원문 단위로 자르므로 중간에서 끊기지 않는다. 줄바꿈 경계를 우선한다."""
    conv = html.escape if escape else (lambda s: s)
    out: list[str] = []
    while text:
        if len(conv(text)) <= size:
            out.append(conv(text))
            break
        # 이스케이프 후 길이가 size 이하인 최대 원문 접두사를 찾는다 (이진 탐색)
        lo, hi = 1, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if len(conv(text[:mid])) <= size:
                lo = mid
            else:
                hi = mid - 1
        cut = text.rfind("\n", 0, lo)
        if cut <= 0:
            cut = lo
        out.append(conv(text[:cut]))
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
        for chunk in _chunks(text, MAX_LEN, escape=escape):
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
