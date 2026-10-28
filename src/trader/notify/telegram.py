"""텔레그램 발신·수신. 토큰·chat_id 가 없으면 NullNotifier(항상 성공)·수신기 None.

토큰이 URL 에 들어가므로 URL·예외 메시지를 로그에 남기지 않는다.
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
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


OUTBOX_TTL = timedelta(hours=24)  # 이보다 오래 못 보낸 알림은 버린다(낡은 경보가 몰려오지 않게)
OUTBOX_FLUSH_MAX = 10  # 한 번에 다시 보내는 건수(429 를 부르지 않게)


class TelegramNotifier:
    """outbox 가 있으면 보내지 못한 청크를 거기 적어 두고 `flush_outbox` 가 다시 보낸다(전체 검토
    2026-10-27: 429·네트워크 오류면 [주문 상태 미확인]·[중요 공시] 같은 알림이 그대로 사라졌다)."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        http: httpx.Client | None = None,
        outbox: Path | None = None,
    ) -> None:
        quiet_httpx()
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._http = http or httpx.Client(timeout=10)
        self._outbox = outbox

    def _post(self, chunk: str) -> bool:
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

    def send(self, text: str, *, escape: bool = True) -> bool:
        """4,000자 단위로 나눠 보낸다(기본 HTML 이스케이프). 전송 실패분은 outbox 에 보관하고
        보관에 성공했으면 True(재전송은 `flush_outbox` 몫 — 호출부가 다시 보내면 중복, 2-T1),
        보관함이 없거나 보관도 실패하면 False + 경고."""
        if not text or not text.strip():
            return True
        chunks = _chunks(text, MAX_LEN, escape=escape)
        for i, chunk in enumerate(chunks):
            if not self._post(chunk):
                return self._queue(chunks[i:])
        return True

    def _queue(self, chunks: list[str]) -> bool:
        """보관 성공이면 True."""
        if self._outbox is None:
            return False
        import uuid

        from trader.platform.filelock import locked

        try:
            self._outbox.parent.mkdir(parents=True, exist_ok=True)
            with locked(self._outbox.with_name(self._outbox.name + ".lock"), timeout=30.0):
                with self._outbox.open("a", encoding="utf-8") as f:
                    at = datetime.now(UTC).isoformat()
                    for c in chunks:
                        row = {"id": uuid.uuid4().hex, "at": at, "chunk": c}
                        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except (OSError, TimeoutError) as e:
            logger.warning("알림 보관 실패: %s", type(e).__name__)
            return False
        return True

    def _read_outbox(self) -> list[dict]:
        out = []
        if not self._outbox or not self._outbox.exists():
            return out
        for line in self._outbox.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def flush_outbox(self, *, now: datetime | None = None) -> dict:
        """보관된 청크를 오래된 순으로 다시 보낸다(최대 OUTBOX_FLUSH_MAX). 첫 실패에서 멈춘다.
        파일은 보낸 뒤에야 지운다 — 재전송 도중 데몬이 죽어도 남은 항목이 사라지지 않게(적어도 한
        번 전달, 드물게 중복). 네트워크 전송은 잠금 밖, 24시간 넘은 것은 버린다(Codex 리뷰)."""
        if self._outbox is None or not self._outbox.exists():
            return {"sent": 0, "left": 0}
        from trader.platform.filelock import locked

        now = now or datetime.now(UTC)
        lock = self._outbox.with_name(self._outbox.name + ".lock")
        with locked(lock, timeout=5.0):
            items = self._read_outbox()
        sent_ids: set[str] = set()
        stale_ids: set[str] = set()
        for it in items:
            key = str(it.get("id") or it.get("at"))
            try:
                old = now - datetime.fromisoformat(str(it.get("at"))) > OUTBOX_TTL
            except ValueError:
                old = True
            if old:
                stale_ids.add(key)
                continue
            if len(sent_ids) >= OUTBOX_FLUSH_MAX:
                break
            if not self._post(str(it.get("chunk") or "")):
                break
            sent_ids.add(key)
        gone = sent_ids | stale_ids
        with locked(lock, timeout=30.0):  # 그사이 새로 보관된 항목은 그대로 둔다
            keep = [k for k in self._read_outbox() if str(k.get("id") or k.get("at")) not in gone]
            if keep:
                self._outbox.write_text(
                    "".join(json.dumps(k, ensure_ascii=False) + "\n" for k in keep),
                    encoding="utf-8",
                )
            else:
                self._outbox.unlink(missing_ok=True)
        return {"sent": len(sent_ids), "left": len(keep), "dropped": len(stale_ids)}


def make_notifier(secrets: Secrets, *, http: httpx.Client | None = None) -> Notifier:
    if secrets.TELEGRAM_BOT_TOKEN and secrets.TELEGRAM_CHAT_ID:
        from trader.platform import paths

        return TelegramNotifier(
            secrets.TELEGRAM_BOT_TOKEN,
            secrets.TELEGRAM_CHAT_ID,
            http=http,
            outbox=paths.state_dir() / "notify_outbox.jsonl",
        )
    return NullNotifier()


# --- 수신 (승인 명령, Phase 4) ------------------------------------------------------

_COMMAND_RE = re.compile(
    r"^/(approve|reject|status|pause|resume|mode|watch|run|kill|daily|fo)(?:@\w+)?(?:\s+(.+?))?\s*$"
)


@dataclass
class Update:
    update_id: int
    text: str
    chat_id: str
    sender_id: str = ""
    # 보낸 사람을 믿을 수 있는가: 1:1 채팅이거나 허용 목록(TELEGRAM_ALLOWED_USERS)에 있는 사람.
    # 그룹 채팅에서 허용 목록 없이 온 명령은 읽기·안전 명령만 처리한다(전체 검토 2026-10-27)
    trusted: bool = True


def parse_command(text: str) -> tuple[str, str | None] | None:
    """`/approve·/reject <id>`, `/status`, `/pause`, `/resume`, `/mode`, `/watch`, `/run` 만
    인식한다."""
    m = _COMMAND_RE.match(text.strip())
    if not m:
        return None
    return m.group(1), m.group(2)


class TelegramReceiver:
    """getUpdates 1회 폴링. 설정된 chat_id 의 메시지 텍스트만 돌려준다."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        http: httpx.Client | None = None,
        allowed_users: set[str] | None = None,
    ) -> None:
        quiet_httpx()
        self._url = f"https://api.telegram.org/bot{token}/getUpdates"
        self._chat_id = str(chat_id)
        self._allowed = {str(u) for u in (allowed_users or set())}
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
            chat_obj = msg.get("chat") or {}
            chat = str(chat_obj.get("id", ""))
            text = msg.get("text")
            if chat != self._chat_id or not isinstance(text, str):
                continue
            sender = str((msg.get("from") or {}).get("id", ""))
            if self._allowed:
                trusted = sender in self._allowed
            else:
                # 종류가 빠진 응답은 1:1 로 간주하지 않는다(Codex 보완 — 보수적 기본값)
                trusted = chat_obj.get("type") == "private"
            out.append(
                Update(update_id=uid, text=text, chat_id=chat, sender_id=sender, trusted=trusted)
            )
        return out


def make_receiver(secrets: Secrets, *, http: httpx.Client | None = None) -> TelegramReceiver | None:
    if secrets.TELEGRAM_BOT_TOKEN and secrets.TELEGRAM_CHAT_ID:
        allowed = {
            x.strip() for x in (secrets.TELEGRAM_ALLOWED_USERS or "").split(",") if x.strip()
        }
        return TelegramReceiver(
            secrets.TELEGRAM_BOT_TOKEN, secrets.TELEGRAM_CHAT_ID, http=http, allowed_users=allowed
        )
    return None
