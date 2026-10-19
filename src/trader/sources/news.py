"""뉴스 수집기: 구글뉴스 RSS(키 없음), 네이버 뉴스 검색 API(키는 헤더).

항목 {title, summary, source, published_at(UTC ISO), url, received_at}.
collect_news() 는 asof 이후 발행분을 제외하고 URL 로 중복을 제거한다.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from trader.config import Market, NewsCfg, Secrets
from trader.sources.base import owned_client
from trader.sources.base import strip_tags as _strip
from trader.sources.base import utc_now as _utc_now

logger = logging.getLogger("trader.sources")

GOOGLE_RSS = "https://news.google.com/rss/search"
# 네이버 검색 API 는 NAVER API HUB(네이버 클라우드)로 이관됐다 (2026-06 출시, 개발자센터는
# 2027-06 종료). 키는 NCP 콘솔 > NAVER API HUB 앱의 Client ID/Secret (개발자센터 키와 호환 안 됨).
NAVER_NEWS = "https://naverapihub.apigw.ntruss.com/search/v1/news"
NAVER_HEADER_ID = "X-NCP-APIGW-API-KEY-ID"
NAVER_HEADER_SECRET = "X-NCP-APIGW-API-KEY"
MACRO_QUERIES: dict[Market, str] = {
    "KR": "코스피 OR 금리 OR 환율 OR 경기",
    "US": "stock market OR Fed OR inflation OR Treasury yields",
}


def _parse_date(s: str | None) -> str | None:
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


class GoogleNewsRss:
    def __init__(self, http: httpx.Client, *, now: Callable[[], datetime] = _utc_now) -> None:
        self._http = http
        self._now = now

    def search(self, query: str, *, lang: str = "ko", max_items: int = 20) -> list[dict]:
        params = (
            {"q": query, "hl": "ko", "gl": "KR", "ceid": "KR:ko"}
            if lang == "ko"
            else {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
        )
        resp = self._http.get(GOOGLE_RSS, params=params, timeout=10)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        received = self._now().isoformat()
        items: list[dict] = []
        for item in root.iter("item"):
            source = item.find("source")
            items.append(
                {
                    "title": _strip(item.findtext("title")),
                    "summary": _strip(item.findtext("description"))[:300],
                    "source": _strip(source.text if source is not None else ""),
                    "published_at": _parse_date(item.findtext("pubDate")),
                    "url": (item.findtext("link") or "").strip(),
                    "received_at": received,
                }
            )
            if len(items) >= max_items:
                break
        return items


class NaverNews:
    def __init__(
        self,
        client_id: str | None,
        client_secret: str | None,
        http: httpx.Client,
        *,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("네이버 검색 API 키가 없습니다")
        self._headers = {
            NAVER_HEADER_ID: client_id.strip(),
            NAVER_HEADER_SECRET: client_secret.strip(),
        }
        self._http = http
        self._now = now

    def search(self, query: str, *, max_items: int = 20) -> list[dict]:
        resp = self._http.get(
            NAVER_NEWS,
            params={"query": query, "display": min(max_items, 100), "sort": "date"},
            headers=self._headers,
            timeout=10,
        )
        resp.raise_for_status()
        received = self._now().isoformat()
        out: list[dict] = []
        for it in resp.json().get("items") or []:
            out.append(
                {
                    "title": _strip(it.get("title")),
                    "summary": _strip(it.get("description"))[:300],
                    "source": "naver",
                    "published_at": _parse_date(it.get("pubDate")),
                    "url": (it.get("originallink") or it.get("link") or "").strip(),
                    "received_at": received,
                }
            )
        return out


def _dedupe_and_filter(items: list[dict], asof: datetime) -> list[dict]:
    cutoff = asof.astimezone(UTC).isoformat()
    seen: set[str] = set()
    out: list[dict] = []
    for it in items:
        url = it.get("url") or ""
        if not url or url in seen:
            continue
        if it.get("published_at") and it["published_at"] > cutoff:
            continue  # 룩어헤드 방지
        seen.add(url)
        out.append(it)
    out.sort(key=lambda i: i.get("published_at") or "", reverse=True)
    return out


def collect_news(
    cfg: NewsCfg,
    secrets: Secrets,
    *,
    ticker: str,
    name: str,
    market: Market,
    asof: datetime,
    http: httpx.Client | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> dict:
    """{"stock": [...], "macro": [...], "sources": {소스: "ok"|오류}}. 소스 실패는 기록하고 계속."""
    with owned_client(http, timeout=10) as http:
        lang = "ko" if market == "KR" else "en"
        stock_query = f"{name} OR {ticker}" if name and name != ticker else ticker
        stock: list[dict] = []
        macro: list[dict] = []
        sources: dict[str, str] = {}

        if cfg.google_rss:
            g = GoogleNewsRss(http, now=now)
            try:
                stock += g.search(stock_query, lang=lang, max_items=cfg.max_items)
                macro += g.search(MACRO_QUERIES[market], lang=lang, max_items=cfg.macro_items)
                sources["google_rss"] = "ok"
            except Exception as e:  # noqa: BLE001 - 소스 단위 격리
                logger.warning("구글뉴스 수집 실패: %s", type(e).__name__)
                sources["google_rss"] = f"{type(e).__name__}: {e}"
        if cfg.naver and market == "KR":
            if not (secrets.NAVER_CLIENT_ID and secrets.NAVER_CLIENT_SECRET):
                sources["naver"] = "no api key"
            else:
                try:
                    n = NaverNews(
                        secrets.NAVER_CLIENT_ID, secrets.NAVER_CLIENT_SECRET, http, now=now
                    )
                    stock += n.search(name or ticker, max_items=cfg.max_items)
                    sources["naver"] = "ok"
                except Exception as e:  # noqa: BLE001
                    logger.warning("네이버 뉴스 수집 실패: %s", type(e).__name__)
                    sources["naver"] = f"{type(e).__name__}: {e}"
        return {
            "stock": _dedupe_and_filter(stock, asof)[: cfg.max_items],
            "macro": _dedupe_and_filter(macro, asof)[: cfg.macro_items],
            "sources": sources,
        }
