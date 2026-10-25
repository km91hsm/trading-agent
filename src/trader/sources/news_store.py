"""시장 전체 뉴스 저장소 (Phase 19 N).

종목이 정해진 뒤 종목별로만 뉴스를 받던 구조로는 "뉴스로 종목을 발굴" 할 수 없다. 데몬이 시장별 질의
묶음(`sources.yaml news.market_queries`)으로 구글뉴스 RSS(+KR 은 네이버 뉴스)를 30분마다 받아 원장
`news_items`(v14) 에 URL 해시로 멱등 적재하고, 발굴(scout)·시장 세션·종목 세션이 이 저장소를 읽는다.

룩어헤드: 저장은 `published_at`·`received_at` 을 모두 남기고 **읽기**(`recent`)가
`published_at <= asof AND received_at <= asof` 로 거른다. 태깅은 유니버스 종목명·코드만 본다
(유니버스 밖 발굴은 scout 의 몫).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from trader.config import NewsCfg, Secrets
from trader.sources.base import owned_client
from trader.sources.base import utc_now as _utc_now
from trader.sources.news import GoogleNewsRss, NaverNews

logger = logging.getLogger("trader.sources")

MIN_NAME_LEN = 2  # 이 길이 미만의 종목명은 태깅하지 않는다 (오탐)
_ASCII = re.compile(r"^[A-Za-z0-9.\-]+$")


def url_hash(url: str, market: str = "") -> str:
    """시장별 멱등 키. 거시 뉴스(티커 없음)는 KR·US 두 행이 될 수 있어 시장을 섞어 센다."""
    return hashlib.sha1(f"{market}|{url.strip()}".encode()).hexdigest()


def tag(text: str, names: dict[str, str]) -> list[str]:
    """본문에 유니버스 종목명(또는 코드)이 들어 있으면 그 코드들. ASCII 티커는 단어 경계로 본다."""
    if not text:
        return []
    low = text.lower()
    out: list[str] = []
    for ticker, name in names.items():
        for needle in {name or "", ticker}:
            needle = needle.strip()
            if len(needle) < MIN_NAME_LEN:
                continue
            if _ASCII.match(needle):
                if re.search(rf"(?<![A-Za-z0-9]){re.escape(needle.lower())}(?![A-Za-z0-9])", low):
                    out.append(ticker)
                    break
            elif needle.lower() in low:
                out.append(ticker)
                break
    return sorted(set(out))


def universe_names(universe: Any, cache_names: dict[tuple[str, str], str], market: str) -> dict:
    """태깅용 {코드: 이름}. KR 은 캐시 이름(없으면 코드만), US 는 심볼(이름은 캐시에 있으면)."""
    out: dict[str, str] = {}
    if market == "KR":
        for t in getattr(universe, "kr", []) or []:
            out[t] = cache_names.get(("KR", t), "")
    else:
        for u in getattr(universe, "us", []) or []:
            out[u.symbol] = cache_names.get(("US", u.symbol), "")
    return out


def insert_items(
    conn: sqlite3.Connection,
    items: list[dict],
    *,
    market: str,
    source: str,
    query: str,
    lang: str,
    names: dict[str, str],
    extra_tickers: bool = False,
) -> int:
    """URL 해시로 멱등 적재. 반환은 새로 들어간 행 수. extra_tickers 면 항목의 `tickers` 도 합친다
    (세이브티커처럼 수집기가 이미 티커를 붙인 소스)."""
    n = 0
    for it in items:
        url = (it.get("url") or "").strip()
        title = (it.get("title") or "").strip()
        if not url or not title:
            continue
        tickers = tag(f"{title} {it.get('summary') or ''}", names)
        if extra_tickers:
            tickers = sorted(set(tickers) | {str(t).strip() for t in it.get("tickers") or [] if t})
        cur = conn.execute(
            "INSERT OR IGNORE INTO news_items(market, source, query, title, url, url_hash, "
            "published_at, received_at, summary, tickers_json, lang) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                market,
                source,
                query,
                title,
                url,
                url_hash(url, market),
                it.get("published_at"),
                it.get("received_at") or _utc_now().isoformat(),
                (it.get("summary") or "")[:300],
                json.dumps(tickers, ensure_ascii=False),
                lang,
            ),
        )
        n += int(cur.rowcount == 1)
    conn.commit()
    return n


def collect(
    conn: sqlite3.Connection,
    cfg: NewsCfg,
    secrets: Secrets,
    *,
    market: str,
    names: dict[str, str],
    http: httpx.Client | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> dict:
    """시장 질의 묶음을 돌려 저장한다. 소스·질의 실패는 `sources` 에 남기고 계속."""
    queries = list(cfg.market_queries.get(market) or [])
    lang = "ko" if market == "KR" else "en"
    inserted = 0
    sources: dict[str, str] = {}
    with owned_client(http, timeout=10) as client:
        if cfg.google_rss:
            g = GoogleNewsRss(client, now=now)
            for q in queries:
                try:
                    items = g.search(q, lang=lang, max_items=cfg.max_items)
                except Exception as e:  # noqa: BLE001 - 질의 단위 격리
                    sources[f"google_rss:{q}"] = f"{type(e).__name__}: {e}"
                    continue
                inserted += insert_items(
                    conn, items, market=market, source="google_rss", query=q, lang=lang, names=names
                )
            sources.setdefault("google_rss", "ok")
        if cfg.naver and market == "KR":
            if not (secrets.NAVER_CLIENT_ID and secrets.NAVER_CLIENT_SECRET):
                sources["naver"] = "no api key"
            else:
                nv = NaverNews(
                    secrets.NAVER_CLIENT_ID, secrets.NAVER_CLIENT_SECRET, client, now=now
                )
                for q in queries:
                    try:
                        items = nv.search(q, max_items=cfg.max_items)
                    except Exception as e:  # noqa: BLE001
                        sources[f"naver:{q}"] = f"{type(e).__name__}: {e}"
                        continue
                    inserted += insert_items(
                        conn, items, market=market, source="naver", query=q, lang=lang, names=names
                    )
                sources.setdefault("naver", "ok")
    return {"market": market, "queries": len(queries), "inserted": inserted, "sources": sources}


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    try:
        d["tickers"] = json.loads(d.pop("tickers_json") or "[]")
    except ValueError:
        d["tickers"] = []
    d.pop("url_hash", None)
    return d


def recent(
    conn: sqlite3.Connection,
    market: str,
    *,
    asof: datetime,
    hours: int = 48,
    limit: int = 60,
    ticker: str | None = None,
) -> list[dict]:
    """asof 기준 최근 hours 시간의 뉴스(최신순). asof 뒤에 발행·수신된 항목은 보이지 않는다."""
    cutoff = asof.astimezone(UTC).isoformat()
    since = (asof.astimezone(UTC) - timedelta(hours=hours)).isoformat()
    sql = (
        "SELECT id, market, source, query, title, url, url_hash, published_at, received_at, "
        "summary, tickers_json, lang FROM news_items WHERE market=? "
        "AND COALESCE(published_at, received_at) <= ? AND received_at <= ? "
        "AND COALESCE(published_at, received_at) >= ?"
    )
    args: list = [market, cutoff, cutoff, since]
    if ticker:
        sql += " AND EXISTS (SELECT 1 FROM json_each(news_items.tickers_json) WHERE value=?)"
        args.append(ticker)
    sql += " ORDER BY COALESCE(published_at, received_at) DESC, id DESC LIMIT ?"
    args.append(limit)
    return [_row(r) for r in conn.execute(sql, args).fetchall()]


def prune(conn: sqlite3.Connection, *, before: datetime) -> int:
    cur = conn.execute(
        "DELETE FROM news_items WHERE COALESCE(published_at, received_at) < ?",
        (before.astimezone(UTC).isoformat(),),
    )
    conn.commit()
    return cur.rowcount


def count(conn: sqlite3.Connection, market: str | None = None) -> int:
    if market:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM news_items WHERE market=?", (market,)
        ).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS n FROM news_items").fetchone()
    return int(row["n"])
