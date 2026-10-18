"""텔레그램 공개채널 수집 (Phase 6): `https://t.me/s/<채널>` 웹 미리보기 HTML 만 쓴다.

Telethon(API id 필요)은 쓰지 않는다. 비공개·미리보기 차단 채널은 빈 결과 + 오류 기록.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

import httpx
from bs4 import BeautifulSoup

from trader.sources.base import strip_tags as _strip
from trader.sources.community import USER_AGENT, _iso_to_utc, _matches

logger = logging.getLogger("trader.sources")

PREVIEW_URL = "https://t.me/s/{channel}"
_CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")


class TelegramChannelSource:
    def __init__(
        self,
        channels: list[str],
        *,
        http: httpx.Client,
        max_posts: int = 30,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        for ch in channels:
            if not _CHANNEL_RE.match(ch):
                raise ValueError(f"텔레그램 채널명 형식 오류: {ch!r}")
        self.channels = channels
        self._http = http
        self.max_posts = max_posts
        self._now = now

    def fetch(self, *, keywords: Iterable[str], asof: datetime) -> dict:
        kws = tuple(keywords)
        cutoff = asof.astimezone(UTC).isoformat()
        sources: dict[str, list[dict]] = {}
        errors: dict[str, str] = {}
        for ch in self.channels:
            label = f"telegram/{ch}"
            try:
                resp = self._http.get(
                    PREVIEW_URL.format(channel=ch), headers={"User-Agent": USER_AGENT}, timeout=10
                )
                resp.raise_for_status()
            except Exception as e:  # noqa: BLE001 - 채널 단위 격리
                errors[label] = f"{type(e).__name__}: {e}"
                continue
            soup = BeautifulSoup(resp.content, "html.parser")
            wraps = soup.select("div.tgme_widget_message_wrap")
            if not wraps:
                logger.warning("텔레그램 미리보기 HTML 구조를 인식하지 못했습니다: %s", ch)
            received = self._now().isoformat()
            posts: list[dict] = []
            for w in wraps:
                text_el = w.select_one("div.tgme_widget_message_text")
                text = _strip(text_el.get_text(" ") if text_el else "")
                if not text or not _matches(text, kws):
                    continue
                t = w.select_one("time[datetime]")
                posted = _iso_to_utc(str(t.get("datetime", "")).strip()) if t else None
                if posted and posted > cutoff:
                    continue
                a = w.select_one("a.tgme_widget_message_date")
                posts.append(
                    {
                        "title": text[:120],
                        "body": text[:500],
                        "author": ch,
                        "posted_at": posted,
                        "url": str(a.get("href", "")) if a else "",
                        "received_at": received,
                        "source": label,
                    }
                )
                if len(posts) >= self.max_posts:
                    break
            sources[label] = posts
        return {"sources": sources, "errors": errors}
