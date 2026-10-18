"""커뮤니티 수집기 (스펙 §0.3 정책).

- 기본 on: Reddit(서브레딧 .rss), 클리앙 주식게시판(목록 1페이지, robots 허용 범위)
- 구현하되 기본 off: DC 마이너갤(kospi·stockus), 에펨코리아 주식게시판 (robots 가 AI 크롤러 차단)
- 조사 후 결정(Phase 6): 토스증권, 네이버 종목토론실

공통: 소스당 요청 간격 ≥ 2초, HTML 구조가 바뀌면 빈 목록 + 경고(예외 아님),
글 시각이 asof 이후면 제외.
항목 {title, body, author, posted_at(UTC ISO), url, received_at, source}.
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

import httpx
from bs4 import BeautifulSoup

from trader.config import KST, CommunityCfg, Market
from trader.sources.base import owned_client
from trader.sources.base import strip_tags as _strip
from trader.sources.base import utc_now as _utc_now

logger = logging.getLogger("trader.sources")

USER_AGENT = (
    "Mozilla/5.0 (compatible; trading-agent/0.1; +https://github.com/km91hsm/trading-agent)"
)
MIN_INTERVAL_S = 2.0
_ATOM = "{http://www.w3.org/2005/Atom}"


def _iso_to_utc(s: str) -> str | None:
    """ISO8601(오프셋 포함) → UTC ISO. 오프셋이 없으면 UTC 로 본다. 해석 불가면 None."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _kst_to_iso(s: str, fmt: str) -> str | None:
    try:
        return datetime.strptime(s.strip(), fmt).replace(tzinfo=KST).astimezone(UTC).isoformat()
    except ValueError:
        return None


def _matches(title: str, keywords: Iterable[str]) -> bool:
    """키워드 일치. ASCII 티커(짧은 영문)는 단어 경계로, 한글 종목명은 부분 일치로 본다."""
    low = title.lower()
    for k in keywords:
        if not k:
            continue
        kl = k.lower()
        if kl.isascii():
            if re.search(rf"(?<![a-z0-9]){re.escape(kl)}(?![a-z0-9])", low):
                return True
        elif kl in low:
            return True
    return False


class _Source:
    name = "base"

    def __init__(
        self,
        http: httpx.Client,
        *,
        now: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._http = http
        self._now = now
        self._sleep = sleep or time.sleep
        self._last: float | None = None

    def _get(self, url: str, **kw) -> httpx.Response:
        if self._last is not None:
            wait = MIN_INTERVAL_S - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = time.monotonic()
        resp = self._http.get(url, headers={"User-Agent": USER_AGENT}, timeout=10, **kw)
        resp.raise_for_status()
        return resp


class Reddit(_Source):
    """auth=(client_id, secret, user) 가 있으면 공식 OAuth API(client_credentials), 없으면 RSS."""

    name = "reddit"

    def __init__(
        self, http: httpx.Client, *, auth: tuple[str, str, str] | None = None, **kw
    ) -> None:
        super().__init__(http, **kw)
        self._auth = auth
        self._token: str | None = None

    def _ua(self) -> str:
        user = self._auth[2] if self._auth else "unknown"
        return f"macos:trader:0.1 (by /u/{user})"

    def _bearer(self) -> str:
        assert self._auth is not None
        if self._token is None:
            resp = self._http.post(
                "https://www.reddit.com/api/v1/access_token",
                data={"grant_type": "client_credentials"},
                auth=(self._auth[0], self._auth[1]),
                headers={"User-Agent": self._ua()},
                timeout=10,
            )
            resp.raise_for_status()
            self._token = str(resp.json().get("access_token") or "")
            if not self._token:
                raise RuntimeError("Reddit 토큰 응답에 access_token 이 없습니다")
        return self._token

    def _fetch_oauth(
        self, subreddit: str, *, keywords: Iterable[str], max_posts: int
    ) -> list[dict]:
        resp = self._http.get(
            f"https://oauth.reddit.com/r/{subreddit}/new",
            params={"limit": 100},
            headers={"Authorization": f"Bearer {self._bearer()}", "User-Agent": self._ua()},
            timeout=10,
        )
        resp.raise_for_status()
        received = self._now().isoformat()
        out: list[dict] = []
        for child in (resp.json().get("data") or {}).get("children") or []:
            d = child.get("data") or {}
            title = _strip(d.get("title"))
            if not _matches(title, keywords):
                continue
            created = d.get("created_utc")
            posted = datetime.fromtimestamp(float(created), tz=UTC).isoformat() if created else None
            out.append(
                {
                    "title": title,
                    "body": _strip(d.get("selftext"))[:500],
                    "author": _strip(d.get("author")),
                    "posted_at": posted,
                    "url": "https://www.reddit.com" + str(d.get("permalink") or ""),
                    "received_at": received,
                    "source": f"reddit/r/{subreddit}",
                }
            )
            if len(out) >= max_posts:
                break
        return out

    def fetch(self, subreddit: str, *, keywords: Iterable[str], max_posts: int = 50) -> list[dict]:
        if self._auth:
            return self._fetch_oauth(subreddit, keywords=keywords, max_posts=max_posts)
        resp = self._get(f"https://www.reddit.com/r/{subreddit}/new/.rss?limit=100")
        root = ET.fromstring(resp.content)
        received = self._now().isoformat()
        out: list[dict] = []
        for entry in root.iter(f"{_ATOM}entry"):
            title = _strip(entry.findtext(f"{_ATOM}title"))
            if not _matches(title, keywords):
                continue
            link = entry.find(f"{_ATOM}link")
            name_el = entry.find(f"{_ATOM}author/{_ATOM}name")
            author = _strip(name_el.text if name_el is not None else "")
            posted = _iso_to_utc((entry.findtext(f"{_ATOM}published") or "").strip())
            out.append(
                {
                    "title": title,
                    "body": _strip(entry.findtext(f"{_ATOM}content"))[:500],
                    "author": author.removeprefix("/u/"),
                    "posted_at": posted,
                    "url": link.get("href", "") if link is not None else "",
                    "received_at": received,
                    "source": f"reddit/r/{subreddit}",
                }
            )
            if len(out) >= max_posts:
                break
        return out


class Clien(_Source):
    name = "clien"

    def fetch(self, *, keywords: Iterable[str], max_posts: int = 50) -> list[dict]:
        resp = self._get("https://www.clien.net/service/board/cm_stock")
        soup = BeautifulSoup(resp.content, "html.parser")
        items = soup.select("div.list_item")
        if not items:
            logger.warning("클리앙 게시판 HTML 구조를 인식하지 못했습니다 (빈 결과)")
            return []
        received = self._now().isoformat()
        out: list[dict] = []
        for it in items:
            a = it.select_one("a.list_subject")
            if a is None:
                continue
            title = _strip(a.get("title") or a.get_text(" "))
            if not _matches(title, keywords):
                continue
            ts = it.select_one("span.timestamp")
            nick = it.select_one("span.nickname")
            href = str(a.get("href", ""))
            out.append(
                {
                    "title": title,
                    "body": "",
                    "author": _strip(nick.get_text(" ") if nick else ""),
                    "posted_at": _kst_to_iso(ts.get_text(), "%Y-%m-%d %H:%M:%S") if ts else None,
                    "url": f"https://www.clien.net{href}" if href.startswith("/") else href,
                    "received_at": received,
                    "source": "clien",
                }
            )
            if len(out) >= max_posts:
                break
        return out


class DcInside(_Source):
    name = "dcinside"

    def fetch(self, gallery: str, *, keywords: Iterable[str], max_posts: int = 50) -> list[dict]:
        resp = self._get(f"https://gall.dcinside.com/mgallery/board/lists/?id={gallery}")
        soup = BeautifulSoup(resp.content, "html.parser")
        rows = soup.select("tr.ub-content")
        if not rows:
            logger.warning("DC 갤러리 HTML 구조를 인식하지 못했습니다 (빈 결과)")
            return []
        received = self._now().isoformat()
        out: list[dict] = []
        for tr in rows:
            a = tr.select_one("td.gall_tit a")
            if a is None:
                continue
            title = _strip(a.get_text(" "))
            if not _matches(title, keywords):
                continue
            date_td = tr.select_one("td.gall_date")
            writer = tr.select_one("td.gall_writer")
            posted = (
                _kst_to_iso(str(date_td.get("title", "")), "%Y-%m-%d %H:%M:%S") if date_td else None
            )
            href = str(a.get("href", ""))
            out.append(
                {
                    "title": title,
                    "body": "",
                    "author": str(writer.get("data-nick", "")) if writer else "",
                    "posted_at": posted,
                    "url": f"https://gall.dcinside.com{href}" if href.startswith("/") else href,
                    "received_at": received,
                    "source": f"dcinside/{gallery}",
                }
            )
            if len(out) >= max_posts:
                break
        return out


class FmKorea(_Source):
    name = "fmkorea"

    def fetch(self, *, keywords: Iterable[str], max_posts: int = 50) -> list[dict]:
        resp = self._get("https://www.fmkorea.com/stock")
        soup = BeautifulSoup(resp.content, "html.parser")
        items = soup.select("li.li")
        if not items:
            logger.warning("에펨코리아 HTML 구조를 인식하지 못했습니다 (빈 결과)")
            return []
        received = self._now().isoformat()
        out: list[dict] = []
        for li in items:
            a = li.select_one("h3.title a")
            if a is None:
                continue
            title = _strip(a.get_text(" "))
            if not _matches(title, keywords):
                continue
            reg = li.select_one("span.regdate")
            author = li.select_one("div.author")
            href = str(a.get("href", ""))
            out.append(
                {
                    "title": title,
                    "body": "",
                    "author": _strip(author.get_text(" ") if author else ""),
                    "posted_at": _kst_to_iso(reg.get_text(), "%Y.%m.%d %H:%M") if reg else None,
                    "url": f"https://www.fmkorea.com{href}" if href.startswith("/") else href,
                    "received_at": received,
                    "source": "fmkorea",
                }
            )
            if len(out) >= max_posts:
                break
        return out


def collect_community(
    cfg: CommunityCfg,
    *,
    ticker: str,
    name: str,
    market: Market,
    asof: datetime,
    http: httpx.Client | None = None,
    now: Callable[[], datetime] = _utc_now,
    sleep: Callable[[float], None] | None = None,
    reddit_auth: tuple[str, str, str] | None = None,
    channels: Any | None = None,
) -> dict:
    """{"posts": [...], "sources": {소스: "ok"|오류|"not available for KR/US"}}.

    channels(ChannelsCfg)가 있으면 YouTube·텔레그램 채널도 같은 posts 에 합친다.
    """
    with owned_client(http, timeout=10) as http:
        keywords = tuple(k for k in (name, ticker) if k)
        posts: list[dict] = []
        sources: dict[str, str] = {}
        kw = {"now": now, "sleep": sleep}

        def run(label: str, fn: Callable[[], list[dict]]) -> None:
            try:
                posts.extend(fn())
                sources[label] = "ok"
            except Exception as e:  # noqa: BLE001 - 소스 단위 격리
                logger.warning("커뮤니티 수집 실패 %s: %s", label, type(e).__name__)
                sources[label] = f"{type(e).__name__}: {e}"

        if cfg.reddit:
            if market == "US":
                r = Reddit(http, auth=reddit_auth, **kw)
                for sub in ("stocks", "wallstreetbets"):
                    run(
                        f"reddit/{sub}",
                        lambda s=sub: r.fetch(s, keywords=keywords, max_posts=cfg.max_posts),
                    )
            else:
                sources["reddit"] = "not available for KR"
        if market == "KR":
            if cfg.clien:
                run(
                    "clien",
                    lambda: Clien(http, **kw).fetch(keywords=keywords, max_posts=cfg.max_posts),
                )
            if cfg.dcinside:
                d = DcInside(http, **kw)
                run(
                    "dcinside/kospi",
                    lambda: d.fetch("kospi", keywords=keywords, max_posts=cfg.max_posts),
                )
            if cfg.fmkorea:
                run(
                    "fmkorea",
                    lambda: FmKorea(http, **kw).fetch(keywords=keywords, max_posts=cfg.max_posts),
                )
        elif cfg.dcinside:
            d = DcInside(http, **kw)
            run(
                "dcinside/stockus",
                lambda: d.fetch("stockus", keywords=keywords, max_posts=cfg.max_posts),
            )

        if channels is not None and (channels.youtube or channels.telegram):
            ch = collect_channels(channels, keywords=keywords, asof=asof, http=http, now=now)
            for label, items in ch["sources"].items():
                posts.extend(items)
                sources[label] = "ok"
            for label, err in ch["errors"].items():
                sources[label] = err

        cutoff = asof.astimezone(UTC).isoformat()
        kept = [p for p in posts if not (p.get("posted_at") and p["posted_at"] > cutoff)]
        kept.sort(key=lambda p: p.get("posted_at") or "", reverse=True)
        return {"posts": kept[: cfg.max_posts], "sources": sources}


def collect_channels(
    channels: Any,
    *,
    keywords: Iterable[str],
    asof: datetime,
    http: httpx.Client,
    now: Callable[[], datetime] = _utc_now,
) -> dict:
    """YouTube·텔레그램 채널 수집을 한데 모은다 ({"sources": {...}, "errors": {...}})."""
    from trader.sources.telegram_channel import TelegramChannelSource
    from trader.sources.youtube import YouTubeSource

    out = {"sources": {}, "errors": {}}
    if channels.youtube:
        r = YouTubeSource(
            list(channels.youtube), http=http, max_videos=channels.max_videos, now=now
        ).fetch(keywords=keywords, asof=asof)
        out["sources"].update(r["sources"])
        out["errors"].update(r["errors"])
    if channels.telegram:
        r = TelegramChannelSource(
            list(channels.telegram), http=http, max_posts=channels.max_posts, now=now
        ).fetch(keywords=keywords, asof=asof)
        out["sources"].update(r["sources"])
        out["errors"].update(r["errors"])
    return out
