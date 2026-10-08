"""커뮤니티 수집기 (스펙 §0.3 정책).

- 기본 on: Reddit(서브레딧 .rss), 클리앙 주식게시판(목록 1페이지, robots 허용 범위)
- 구현하되 기본 off: DC 마이너갤(kospi·stockus), 에펨코리아 주식게시판 (robots 가 AI 크롤러 차단)
- 조사 후 결정(Phase 6): 토스증권, 네이버 종목토론실

공통: 소스당 요청 간격 ≥ 2초, HTML 구조가 바뀌면 빈 목록 + 경고(예외 아님),
글 시각이 asof 이후면 제외.
항목 {title, body, author, posted_at(UTC ISO), url, received_at, source}.
"""

from __future__ import annotations

import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

from trader.config import CommunityCfg, Market

logger = logging.getLogger("trader.sources")

USER_AGENT = (
    "Mozilla/5.0 (compatible; trading-agent/0.1; +https://github.com/km91hsm/trading-agent)"
)
MIN_INTERVAL_S = 2.0
_KST = ZoneInfo("Asia/Seoul")
_TAG_RE = re.compile(r"<[^>]+>")
_ATOM = "{http://www.w3.org/2005/Atom}"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _strip(text: str | None) -> str:
    return html.unescape(_TAG_RE.sub("", html.unescape(text or ""))).strip()


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
        return datetime.strptime(s.strip(), fmt).replace(tzinfo=_KST).astimezone(UTC).isoformat()
    except ValueError:
        return None


def _matches(title: str, keywords: Iterable[str]) -> bool:
    low = title.lower()
    return any(k and k.lower() in low for k in keywords)


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
    name = "reddit"

    def fetch(self, subreddit: str, *, keywords: Iterable[str], max_posts: int = 50) -> list[dict]:
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
) -> dict:
    """{"posts": [...], "sources": {소스: "ok"|오류|"not available for KR/US"}}."""
    http = http or httpx.Client(timeout=10)
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
            r = Reddit(http, **kw)
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
                "clien", lambda: Clien(http, **kw).fetch(keywords=keywords, max_posts=cfg.max_posts)
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

    cutoff = asof.astimezone(UTC).isoformat()
    kept = [p for p in posts if not (p.get("posted_at") and p["posted_at"] > cutoff)]
    kept.sort(key=lambda p: p.get("posted_at") or "", reverse=True)
    return {"posts": kept[: cfg.max_posts], "sources": sources}
