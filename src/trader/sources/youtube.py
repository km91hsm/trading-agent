"""YouTube 채널 자막 수집 (스펙 §3, Phase 6): 채널 RSS 최신 영상 → 키워드 일치 영상의 자막 발췌.

API 키 없이 공개 RSS(`feeds/videos.xml?channel_id=`)와 youtube-transcript-api 를 쓴다.
자막 실패는 영상 단위로 errors 에 남기고 계속한다.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

import httpx

from trader.sources.base import strip_tags as _strip
from trader.sources.community import _iso_to_utc, _matches

logger = logging.getLogger("trader.sources")

RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel}"
EXCERPT_CHARS = 2000
_ATOM = "{http://www.w3.org/2005/Atom}"
_YT = "{http://www.youtube.com/xml/schemas/2015}"
_CHANNEL_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_MEDIA = "{http://search.yahoo.com/mrss/}"


TRANSCRIPT_TIMEOUT_S = 15.0


def default_transcript(video_id: str) -> str:
    """youtube-transcript-api 로 한국어·영어 자막을 받아 이어 붙인다. 라이브러리의 requests
    세션은 타임아웃이 없어(TLS 읽기가 멈추면 analyze 잡이 heavy 락을 쥔 채 영원히 기다린다)
    기본 타임아웃을 주는 세션을 넣는다."""
    import requests
    from youtube_transcript_api import YouTubeTranscriptApi

    class _TimeoutSession(requests.Session):
        def request(self, *a, **kw):  # type: ignore[override]
            kw.setdefault("timeout", TRANSCRIPT_TIMEOUT_S)
            return super().request(*a, **kw)

    fetched = YouTubeTranscriptApi(http_client=_TimeoutSession()).fetch(
        video_id, languages=["ko", "en"]
    )
    return " ".join(s.text for s in fetched)


class YouTubeSource:
    def __init__(
        self,
        channels: list[str],
        *,
        http: httpx.Client,
        max_videos: int = 3,
        transcript_fn: Callable[[str], str] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        for ch in channels:
            if not _CHANNEL_RE.match(ch):
                raise ValueError(f"YouTube 채널 ID 형식 오류: {ch!r}")
        self.channels = channels
        self._http = http
        self.max_videos = max_videos
        self._transcript = transcript_fn or default_transcript
        self._now = now

    def fetch(self, *, keywords: Iterable[str], asof: datetime) -> dict:
        """{"sources": {"youtube/<채널>": [항목]}, "errors": {"youtube/<채널>": 메시지}}."""
        kws = tuple(keywords)
        cutoff = asof.astimezone(UTC).isoformat()
        sources: dict[str, list[dict]] = {}
        errors: dict[str, str] = {}
        for ch in self.channels:
            label = f"youtube/{ch}"
            try:
                resp = self._http.get(RSS_URL.format(channel=ch), timeout=10)
                resp.raise_for_status()
                root = ET.fromstring(resp.content)
            except Exception as e:  # noqa: BLE001 - 채널 단위 격리
                errors[label] = f"{type(e).__name__}: {e}"
                continue
            received = self._now().isoformat()
            items: list[dict] = []
            failures: list[str] = []
            for entry in root.iter(f"{_ATOM}entry"):
                vid = (entry.findtext(f"{_YT}videoId") or "").strip()
                title = _strip(entry.findtext(f"{_ATOM}title"))
                desc = _strip(entry.findtext(f"{_MEDIA}group/{_MEDIA}description"))
                published = _iso_to_utc((entry.findtext(f"{_ATOM}published") or "").strip())
                if published and published > cutoff:
                    continue
                if not _matches(f"{title} {desc}", kws):
                    continue
                try:
                    excerpt = self._transcript(vid)[:EXCERPT_CHARS]
                except Exception as e:  # noqa: BLE001 - 영상 단위 격리
                    failures.append(f"{vid}: {type(e).__name__}: {e}")
                    excerpt = ""
                link = entry.find(f"{_ATOM}link")
                items.append(
                    {
                        "video_id": vid,
                        "title": title,
                        "body": desc[:500],
                        "author": _strip(entry.findtext(f"{_ATOM}author/{_ATOM}name")),
                        "posted_at": published,
                        "url": link.get("href", "") if link is not None else "",
                        "transcript_excerpt": excerpt,
                        "received_at": received,
                        "source": label,
                    }
                )
                if len(items) >= self.max_videos:
                    break
            sources[label] = items
            if failures:
                errors[label] = "; ".join(failures)
        return {"sources": sources, "errors": errors}
