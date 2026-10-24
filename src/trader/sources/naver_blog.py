"""네이버 블로그 공개 RSS 수집 (2026-10-24): `rss.blog.naver.com/<id>.xml` 의 최근 글.

키워드 매칭을 하지 않고 최근 N건을 늘 싣는다 — 거시·산업 글이 많은 블로그는 티커 매칭이면 거의 비기
때문이다. 분위기 애널리스트 입력(`sentiment.community.sources.naver_blog/<id>`)으로만 들어간다.
룩어헤드: pubDate <= asof. 블로그 단위로 실패를 격리한다.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from trader.sources.base import strip_tags as _strip

logger = logging.getLogger("trader.sources")

RSS_URL = "https://rss.blog.naver.com/{blog}.xml"
EXCERPT_CHARS = 2000
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")


def _rfc822_to_iso(s: str | None) -> str | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s.strip()).astimezone(UTC).isoformat()
    except (TypeError, ValueError):
        return None


class NaverBlogSource:
    def __init__(
        self,
        blogs: list[str],
        *,
        http: httpx.Client,
        max_posts: int = 5,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        for b in blogs:
            if not _ID_RE.match(b):
                raise ValueError(f"네이버 블로그 id 형식 오류: {b!r}")
        self.blogs = blogs
        self._http = http
        self.max_posts = max_posts
        self._now = now

    def fetch(self, *, asof: datetime, keywords=None) -> dict:
        """{"sources": {"naver_blog/<id>": [항목]}, "errors": {...}}. keywords 는 무시한다."""
        cutoff = asof.astimezone(UTC).isoformat()
        sources: dict[str, list[dict]] = {}
        errors: dict[str, str] = {}
        for blog in self.blogs:
            label = f"naver_blog/{blog}"
            try:
                resp = self._http.get(RSS_URL.format(blog=blog), timeout=10)
                resp.raise_for_status()
                root = ET.fromstring(resp.content)
            except Exception as e:  # noqa: BLE001 - 블로그 단위 격리
                errors[label] = f"{type(e).__name__}: {e}"
                continue
            received = self._now().isoformat()
            items: list[dict] = []
            for item in root.iter("item"):
                posted = _rfc822_to_iso(item.findtext("pubDate"))
                if posted and posted > cutoff:
                    continue
                items.append(
                    {
                        "title": _strip(item.findtext("title")),
                        "body": _strip(item.findtext("description"))[:EXCERPT_CHARS],
                        "author": _strip(item.findtext("author")) or blog,
                        "category": _strip(item.findtext("category")),
                        "posted_at": posted,
                        "url": (item.findtext("link") or "").strip(),
                        "received_at": received,
                        "source": label,
                    }
                )
                if len(items) >= self.max_posts:
                    break
            sources[label] = items
        return {"sources": sources, "errors": errors}
