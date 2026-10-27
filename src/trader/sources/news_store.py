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
from difflib import SequenceMatcher
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
            "published_at, received_at, summary, tickers_json, lang, severity) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                it.get("severity"),  # 공시 중요도(Phase 25): critical|high|info|None
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


# --- 유사 뉴스 판정 (Phase 23) --------------------------------------------------------
_TITLE_TAIL = re.compile(r"\s*[-–—|│]\s*[^-–—|│]{1,20}$")  # 제목 끝 ' - 매체명'
_TITLE_NOISE = re.compile(r"[\[\(【][^\]\)】]*[\]\)】]")  # [단독]·(종합)·【속보】
_TOKEN = re.compile(r"[0-9a-z가-힣]+")
_STOP = {
    "속보",
    "단독",
    "종합",
    "기자",
    "뉴스",
    "및",
    "the",
    "a",
    "an",
    "to",
    "of",
    "in",
    "on",
    "for",
}


def normalize_title(title: str) -> str:
    """유사 판정용 제목 정규화: 소문자, 괄호 머리말·끝 매체명 제거, 기호 제거. 숫자는 남긴다
    (가격·분기가 다르면 다른 사건)."""
    t = _TITLE_NOISE.sub(" ", title or "")
    t = _TITLE_TAIL.sub("", t)
    return " ".join(_TOKEN.findall(t.lower()))


def title_tokens(title: str) -> set[str]:
    return {w for w in normalize_title(title).split() if w not in _STOP and len(w) > 1}


def _features(title: str) -> tuple[str, set[str], set[str]]:
    """(정규화 제목, 토큰, 숫자 토큰) — 유사 판정에 쓰는 것을 한 번만 계산한다."""
    norm = normalize_title(title)
    toks = {w for w in norm.split() if w not in _STOP and len(w) > 1}
    return norm, toks, {t for t in toks if t[0].isdigit()}


_Feat = tuple[str, set[str], set[str]]


def _char_multiset(s: str) -> frozenset[tuple[str, int]]:
    """글자 다중집합을 (글자, k번째) 집합으로. 두 집합의 교집합 크기 = 다중집합 교집합 크기라
    `SequenceMatcher.quick_ratio()` 의 matches 를 C 수준 집합 연산 한 번으로 얻는다."""
    seen: dict[str, int] = {}
    out = []
    for c in s:
        k = seen.get(c, 0) + 1
        seen[c] = k
        out.append((c, k))
    return frozenset(out)


def _similar_f(fa: _Feat, fb: _Feat, *, jaccard: float, ratio: float) -> bool:
    """유사 판정의 정의(문서화된 규칙). `_Pool.has_similar` 는 같은 판정을 색인으로 빠르게 한다."""
    na, ta, da = fa
    nb, tb, db = fb
    if da != db:
        return False
    if ta and tb and len(ta & tb) / len(ta | tb) >= jaccard:
        return True
    if not na or not nb:
        return False
    return SequenceMatcher(None, na, nb).ratio() >= ratio


def is_similar(a: str, b: str, *, jaccard: float = 0.6, ratio: float = 0.8) -> bool:
    """두 제목이 같은 내용인가: 토큰 Jaccard ≥ jaccard 또는 문자열 유사도 ≥ ratio. 숫자 토큰
    (분기·금액·가격)이 다르면 다른 사건으로 본다."""
    return _similar_f(_features(a), _features(b), jaccard=jaccard, ratio=ratio)


class _Pool:
    """비교 풀 하나(태그 키 × 숫자 토큰 묶음). `_similar_f` 가 참일 수 있는 항목만 후보로 꺼낸다.

    - 숫자 토큰이 다르면 거짓이므로 풀 자체를 숫자 토큰 집합으로 나눈다(바깥 dict).
    - Jaccard ≥ jaccard(>0) 은 토큰을 하나 이상 공유해야 하므로 토큰 역색인으로 찾는다.
    - SequenceMatcher ≥ ratio(>0) 는 2·min(길이)/(길이 합) ≥ ratio 여야 하므로 길이 창으로 찾는다.
    후보의 최종 판정은 `_similar_f` 그대로라 결과는 전수 비교와 같다(임계값이 0 이하이면 전수)."""

    __slots__ = ("items", "by_tok", "by_len", "sms", "mss")

    def __init__(self) -> None:
        self.items: list[_Feat] = []
        self.sms: dict[int, SequenceMatcher] = {}  # 항목별 seq2 고정 matcher(처음 쓸 때 만든다)
        self.mss: list[frozenset] = []  # 항목별 글자 다중집합
        self.by_tok: dict[str, list[int]] = {}
        self.by_len: dict[int, list[int]] = {}

    def add(self, f: _Feat) -> None:
        i = len(self.items)
        self.items.append(f)
        self.mss.append(_char_multiset(f[0]))
        for t in f[1]:
            self.by_tok.setdefault(t, []).append(i)
        self.by_len.setdefault(len(f[0]), []).append(i)

    def has_similar(self, f: _Feat, *, jaccard: float, ratio: float) -> bool:
        """`_similar_f(f, p)` 가 참인 풀 항목 p 가 있는가(숫자 토큰은 풀 단위로 이미 같다).
        같은 정수·같은 식으로 계산하므로 판정은 `_similar_f` 와 같다(테스트가 전수 비교로 확인)."""
        if jaccard <= 0 or ratio <= 0:
            return any(_similar_f(f, p, jaccard=jaccard, ratio=ratio) for p in self.items)
        na, ta, _ = f
        items = self.items
        # ① Jaccard: 토큰을 공유한 항목만, 공유 수는 역색인에서 센다
        if ta:
            inter: dict[int, int] = {}
            for t in ta:
                for i in self.by_tok.get(t, ()):
                    inter[i] = inter.get(i, 0) + 1
            nt = len(ta)
            for i, c in inter.items():
                tb = items[i][1]
                if c / (nt + len(tb) - c) >= jaccard:
                    return True
        # ② SequenceMatcher: 길이 창(real_quick_ratio) → 글자 다중집합(quick_ratio) → ratio
        la = len(na)
        if not la:
            return False
        # 2·min/(la+lb) ≥ r ⇔ la·r/(2−r) ≤ lb ≤ la·(2−r)/r. 부동소수 경계는 한 칸 넓혀 잡는다
        lo = max(1, int(la * ratio / (2 - ratio)) - 1) if ratio < 2 else la
        hi = int(la * (2 - ratio) / ratio) + 1
        msa: frozenset | None = None
        sms, mss = self.sms, self.mss
        for n, ids in self.by_len.items():
            if n < lo or n > hi:
                continue
            length = la + n
            if 2.0 * min(la, n) / length < ratio:
                continue
            if msa is None:
                msa = _char_multiset(na)
            for i in ids:
                if 2.0 * len(msa & mss[i]) / length < ratio:
                    continue
                sm = sms.get(i)
                if sm is None:
                    sm = sms[i] = SequenceMatcher(None, "", items[i][0])
                sm.set_seq1(na)
                if sm.ratio() >= ratio:
                    return True
        return False


MAX_PENDING = 1000  # novel_items 한 번에 판정하는 미판정 항목 상한(최신부터)


def novel_items(
    conn: sqlite3.Connection,
    market: str,
    *,
    since: datetime,
    lookback_days: int = 3,
    jaccard: float = 0.6,
    ratio: float = 0.8,
    max_pending: int = MAX_PENDING,
) -> list[dict]:
    """`since` 이후 받은 미판정 항목을 앞선 항목(최근 lookback_days, 같은 시장)과 비교해
    `novel`(1 새 내용 / 0 유사)을 저장하고 **새 내용인 항목**만 돌려준다. 비교 풀은 같은 태그
    종목끼리(태그 없는 뉴스는 태그 없는 풀), 표준 라이브러리만 쓴다(LLM 없음).

    판정은 전부 메모리에서 끝낸 뒤 쓰기는 `executemany` 한 번으로 짧게 커밋한다 — 판정 루프 동안
    쓰기 잠금을 잡고 있으면 손절 기록·결정 제출이 busy timeout 에 걸린다(전체 검토 P3). 제목
    정규화·토큰은 항목마다 한 번만 계산한다.

    성능(전체 검토 3차): 풀은 `_Pool` 색인(숫자 토큰 묶음·토큰 역색인·길이 창)으로 후보만 비교한다.
    미판정 항목이 `max_pending` 을 넘으면(데몬 재시작 뒤 넓은 since 창) **최신 max_pending 건만**
    판정하고, 더 오래된 밀린 항목은 비교 없이 `novel=0`(새 내용 아님)으로 닫는다 — 장중 트리거는
    지금 막 들어온 뉴스가 목적이고 오래된 기사는 아침 scan 이 48시간 뉴스로 본다. 닫힌 항목도
    이번 호출의 비교 풀에는 앞선 항목으로 넣는다(그와 같은 최신 기사는 유사)."""
    lo = (since.astimezone(UTC) - timedelta(days=lookback_days)).isoformat()
    rows = conn.execute(
        "SELECT id, title, tickers_json, received_at, novel FROM news_items "
        "WHERE market=? AND received_at >= ? ORDER BY received_at, id",
        (market, lo),
    ).fetchall()
    if conn.in_transaction:
        conn.commit()  # 읽기 트랜잭션을 판정 루프 동안 붙잡지 않는다
    since_iso = since.astimezone(UTC).isoformat()
    pools: dict[tuple[str, frozenset[str]], _Pool] = {}

    def add(keys: list[str], f: _Feat) -> None:
        digits = frozenset(f[2])
        for key in keys:
            pool = pools.get((key, digits))
            if pool is None:
                pool = pools[(key, digits)] = _Pool()
            pool.add(f)

    pending: list[sqlite3.Row] = []
    for r in rows:
        if r["received_at"] >= since_iso and r["novel"] is None:
            pending.append(r)
            continue
        if r["novel"] == 0:
            continue  # 유사 기사로 판정된 것은 대표가 아니다(대표 제목만 풀에)
        add(_pool_keys(r["tickers_json"]), _features(r["title"]))
    updates: list[tuple[int, int]] = []
    cut = max(0, len(pending) - max(0, max_pending))
    if cut:
        logger.info(
            "뉴스 유사 판정 %s: 미판정 %d건 중 오래된 %d건은 비교 없이 닫음",
            market,
            len(pending),
            cut,
        )
        for r in pending[:cut]:
            updates.append((0, r["id"]))
            add(_pool_keys(r["tickers_json"]), _features(r["title"]))
    novel_ids: list[int] = []
    for r in pending[cut:]:
        keys = _pool_keys(r["tickers_json"])
        f = _features(r["title"])
        digits = frozenset(f[2])
        dup = False
        for key in keys:
            pool = pools.get((key, digits))
            if pool is not None and pool.has_similar(f, jaccard=jaccard, ratio=ratio):
                dup = True
                break
        updates.append((0 if dup else 1, r["id"]))
        if not dup:
            novel_ids.append(int(r["id"]))
            add(keys, f)
    if updates:
        conn.executemany("UPDATE news_items SET novel=? WHERE id=?", updates)
        conn.commit()
    out: list[dict] = []
    for i in range(0, len(novel_ids), 500):
        chunk = novel_ids[i : i + 500]
        qs = ",".join("?" * len(chunk))
        found = conn.execute(
            "SELECT id, market, source, query, title, url, url_hash, published_at, "
            f"received_at, summary, tickers_json, lang FROM news_items WHERE id IN ({qs})",
            chunk,
        ).fetchall()
        by_id = {int(row["id"]): row for row in found}
        out += [_row(by_id[x]) for x in chunk if x in by_id]
    return out


def _pool_keys(tickers_json: str | None) -> list[str]:
    try:
        tickers = json.loads(tickers_json or "[]")
    except ValueError:
        tickers = []
    return [str(t) for t in tickers] or [""]


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
