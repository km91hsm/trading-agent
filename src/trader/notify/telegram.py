"""텔레그램 발신·수신. 토큰·chat_id 가 없으면 NullNotifier(항상 성공)·수신기 None.

토큰이 URL 에 들어가므로 URL·예외 메시지를 로그에 남기지 않는다.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from typing import Protocol

import httpx

from trader.config import Secrets
from trader.logging_setup import quiet_httpx

logger = logging.getLogger("trader.notify")

MAX_LEN = 4000


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
        quiet_httpx()
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


# --- 수신 (승인 명령, Phase 4) ------------------------------------------------------

_COMMAND_RE = re.compile(
    r"^/(approve|reject|status|pause|resume|mode|watch|run|kill)(?:@\w+)?(?:\s+(.+?))?\s*$"
)


@dataclass
class Update:
    update_id: int
    text: str
    chat_id: str


def parse_command(text: str) -> tuple[str, str | None] | None:
    """`/approve·/reject <id>`, `/status`, `/pause`, `/resume`, `/mode`, `/watch`, `/run` 만
    인식한다."""
    m = _COMMAND_RE.match(text.strip())
    if not m:
        return None
    return m.group(1), m.group(2)


class TelegramReceiver:
    """getUpdates 1회 폴링. 설정된 chat_id 의 메시지 텍스트만 돌려준다."""

    def __init__(self, token: str, chat_id: str, *, http: httpx.Client | None = None) -> None:
        quiet_httpx()
        self._url = f"https://api.telegram.org/bot{token}/getUpdates"
        self._chat_id = str(chat_id)
        self._http = http or httpx.Client(timeout=15)
        # 마지막 poll 원본 결과(걸러진 것 포함)의 최대 update_id. 호출자가 오프셋에 반영한다.
        # 다른 채팅 업데이트만 100건 쌓이면 getUpdates 가 같은 묶음만 돌려줘 명령이 영원히 안 온다.
        self.last_update_id: int | None = None

    def poll(self, offset: int | None = None) -> list[Update]:
        params: dict[str, str] = {"timeout": "0", "allowed_updates": '["message"]'}
        if offset is not None:
            params["offset"] = str(offset)
        try:
            resp = self._http.get(self._url, params=params)
        except httpx.HTTPError as e:
            logger.warning("텔레그램 수신 실패: %s", type(e).__name__)
            return []
        if resp.status_code != 200:
            logger.warning("텔레그램 수신 거부: HTTP %s", resp.status_code)
            return []
        try:
            body = resp.json()
        except ValueError:
            return []
        out: list[Update] = []
        self.last_update_id = None
        for item in body.get("result") or []:
            try:
                uid = int(item["update_id"])
            except (KeyError, TypeError, ValueError):
                continue
            self.last_update_id = max(self.last_update_id or uid, uid)
            msg = item.get("message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            text = msg.get("text")
            if chat != self._chat_id or not isinstance(text, str):
                continue
            out.append(Update(update_id=uid, text=text, chat_id=chat))
        return out


def make_receiver(secrets: Secrets, *, http: httpx.Client | None = None) -> TelegramReceiver | None:
    if secrets.TELEGRAM_BOT_TOKEN and secrets.TELEGRAM_CHAT_ID:
        return TelegramReceiver(secrets.TELEGRAM_BOT_TOKEN, secrets.TELEGRAM_CHAT_ID, http=http)
    return None
