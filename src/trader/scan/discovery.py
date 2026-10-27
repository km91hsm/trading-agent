"""뉴스 기반 종목 발굴 (Phase 19 S).

scan 의 screener 세션 **안에서** scout 서브에이전트가 시장 전체 뉴스 저장소(news_items)·시장 메모를
읽고 "오늘 분석할 가치가 있는 종목" 을 발굴한다. 순서는 ① 유니버스 안(뉴스가 가리키는 유니버스
종목) ② 상장 종목 전체(유니버스 밖, 최대 `MAX_LISTED`). LLM 은 이름·촉매만 내고 **코드 확정·거래소·
제외·병합은 파이썬**이 한다(`resolve`·`merge`). 세션 수는 늘지 않는다(screen 세션 1회 안의 단계).

파일(시장 단위, `market_dir`): `_discovery_inputs`(파이썬이 세션 앞에서) →
`_discovery`(submit-discovery).
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from trader.config import NewsCfg, Settings, Universe
from trader.orchestrator.run_store import market_dir, read_checkpoint, write_checkpoint

logger = logging.getLogger("trader.scan")

MAX_LISTED = 2  # 유니버스 밖 발굴 종목은 하루 최대 2개(피처·섹터가 없어 보수적으로)
RECENT_PICK_DAYS = 5  # 최근 운영일의 picks·발굴을 입력에 실어 같은 종목 반복을 억제
US_EXCHANGES = ("NAS", "NYS", "AMS")
_KR_CODE = re.compile(r"^\d{6}$")
_US_SYM = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_STRIP = re.compile(
    r"\(주\)|㈜|주식회사|\b(inc|corp|corporation|co|ltd|limited|plc|holdings?|the|company)\b\.?",
    re.IGNORECASE,
)
_PUNCT = re.compile(r"[^0-9a-z가-힣]+")


def normalize(name: str) -> str:
    """상호 정규화: 공백·㈜·(주)·Inc/Corp/Co/Ltd 제거, 소문자, 기호 제거."""
    s = _STRIP.sub(" ", name or "")
    return _PUNCT.sub("", s.lower())


def reverse_index(cache_names: dict[tuple[str, str], str], market: str) -> dict[str, str]:
    """정규화한 이름 → 코드 (캐시 JSON: KR 은 DART corpCode, US 는 EDGAR 티커 표). 같은 이름이
    둘이면 먼저 본 것(상장사 코드 맵은 대부분 유일)."""
    out: dict[str, str] = {}
    for (m, ticker), name in cache_names.items():
        if m != market:
            continue
        key = normalize(name)
        if key and key not in out:
            out[key] = ticker
    return out


def _recent_picks(market: str, today: date, state: Path | None) -> list[dict]:
    out: list[dict] = []
    for i in range(1, RECENT_PICK_DAYS + 3):  # 주말 포함 7일을 봐 운영일 5개쯤
        d = today - timedelta(days=i)
        scan = read_checkpoint(market_dir(d, market, state), "_scan") or {}
        if not scan:
            continue
        picks = [p.get("ticker") for p in scan.get("picks") or []]
        disc = [c.get("ticker") for c in ((scan.get("discovery") or {}).get("candidates") or [])]
        out.append({"date": d.isoformat(), "picks": picks, "discovered": [t for t in disc if t]})
        if len(out) >= RECENT_PICK_DAYS:
            break
    return out


def build_inputs(
    conn: sqlite3.Connection,
    settings: Settings,
    universe: Universe,
    *,
    market: str,
    today: date,
    asof: datetime,
    quant_candidates: list[dict],
    held: list[str] | None = None,
    state: Path | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(),  # noqa: DTZ005 - 호출자가 UTC 를 넘긴다
    news_cfg: NewsCfg | None = None,
    cache_names: dict[tuple[str, str], str] | None = None,
    sources_cfg: Any | None = None,
    http: Any | None = None,
    intraday_news: list[dict] | None = None,
    analyzed_today: list[str] | None = None,
) -> dict:
    """scout 입력을 조립해 `_discovery_inputs` 로 저장하고 돌려준다. 뉴스가 비어도 파일은 만든다
    (scout 이 '발굴 없음' 을 낼 수 있게). `sources_cfg.channels.naver_blog` + `http` 가 있으면
    블로그 최근 글(`blog_posts`)도 싣는다(KR 만, 실패는 빈 목록).

    `intraday_news` 가 있으면 장중 발굴(Phase 23): 저장소 48h 대신 **새 내용으로 판정된 뉴스만**
    싣고 `intraday_round`(오늘 n번째) 를 붙이며 `recent_picks` 맨 앞에 오늘 이미 분석·발굴한
    종목(`analyzed_today`)을 넣는다. 파일은 `_discovery_inputs`(도구가 읽는 자리)와 사본
    `_discovery_inputs_r<n>` 둘."""
    from trader.config import load_sectors
    from trader.risk import regime as regime_mod
    from trader.sources import manual_inputs, news_store

    cfg = news_cfg or NewsCfg()
    names = cache_names or {}
    try:
        sectors = load_sectors()
    except Exception:  # noqa: BLE001 - 섹터 맵은 보조 정보
        sectors = None
    if market == "KR":
        members = [(t, None) for t in universe.kr]
    else:
        members = [(u.symbol, u.exchange) for u in universe.us]
    uni = [
        {
            "ticker": t,
            "exchange": e,
            "name": names.get((market, t), ""),
            "sector": sectors.sector_of(t, market) if sectors else None,  # type: ignore[arg-type]
        }
        for t, e in members
        if t not in set(universe.exclude)
    ]
    if intraday_news is not None:
        news = intraday_news
    else:
        news = news_store.recent(
            conn, market, asof=asof, hours=cfg.store_hours, limit=cfg.store_items
        )
    news_rows = [
        {
            "id": n["id"],
            "title": n["title"],
            "summary": (n.get("summary") or "")[:200],
            "source": n.get("source"),
            "published_at": n.get("published_at"),
            "tickers": n.get("tickers") or [],
        }
        for n in news
    ]
    try:
        notes = manual_inputs.read_market(market, asof=asof, state=state)
    except Exception as e:  # noqa: BLE001
        logger.debug("시장 메모 읽기 실패 %s: %s", market, e)
        notes = []
    reg = regime_mod.load(conn, market) or {}
    saveticker_snap: dict | None = None
    try:
        from trader.sources import saveticker

        root = saveticker.resolve_dir(getattr(cfg, "saveticker_dir", None))
        snap = saveticker.snapshot(root, asof=asof)
        if snap:
            saveticker_snap = {
                k: snap.get(k) for k in ("observed_at", "movers", "community_top5", "report")
            }
    except Exception as e:  # noqa: BLE001 - 선택 입력
        logger.debug("세이브티커 발굴 입력 실패 %s: %s", market, e)
    blog_posts = _blog_posts(sources_cfg, http, market=market, asof=asof, now=now)
    d = market_dir(today, market, state, create=True)
    recent = _recent_picks(market, today, state)
    rnd = None
    if intraday_news is not None:
        rnd = 1 + sum(1 for p in d.glob("_discovery_inputs_r*.json"))
        recent = [
            {
                "date": today.isoformat(),
                "picks": sorted(set(analyzed_today or [])),
                "discovered": [],
            },
            *recent,
        ]
    data = {
        "scope": "discovery",
        "market": market,
        "date": today.isoformat(),
        "asof": asof.isoformat(),
        "universe": uni,
        "news": news_rows,
        "market_notes": notes,
        "regime": {"state": reg.get("state") or "bull", "count": reg.get("count") or 0},
        "held": sorted(set(held or [])),
        "recent_picks": recent,
        "intraday_round": rnd,
        "saveticker": saveticker_snap,
        "theme_heat": _theme_heat(conn, settings, market, today),
        "blog_posts": blog_posts,
        "quant_candidates": [
            {k: c.get(k) for k in ("ticker", "exchange", "score", "reasons")}
            for c in quant_candidates[:30]
        ],
        "max_listed": MAX_LISTED,
        **_event_watch(conn, market, today, asof),
    }
    write_checkpoint(d, "_discovery_inputs", {"schema_version": 1, "data": data, "call": None})
    if rnd is not None:
        write_checkpoint(
            d, f"_discovery_inputs_r{rnd}", {"schema_version": 1, "data": data, "call": None}
        )
    return data


def news_burst(
    novel: list[dict], *, universe_tickers: set[str], held: set[str], min_items: int = 1
) -> dict:
    """새 내용 뉴스(novel_items 결과)를 태그 종목별로 센다. 반환
    {"tickers": {코드: [뉴스]} (유니버스·보유 종목이고 ≥ min_items), "untagged": 태그 없는 건수,
    "total": 전체}. 유니버스 밖 태그는 없다(태깅이 유니버스만 하므로)."""
    by: dict[str, list[dict]] = {}
    untagged = 0
    for n in novel:
        tickers = [t for t in (n.get("tickers") or []) if t in universe_tickers or t in held]
        if not tickers:
            untagged += 1
        for t in tickers:
            by.setdefault(t, []).append(n)
    return {
        "tickers": {t: items for t, items in by.items() if len(items) >= min_items},
        "untagged": untagged,
        "total": len(novel),
    }


def _blog_posts(
    sources_cfg: Any, http: Any, *, market: str, asof: datetime, now: Callable[[], datetime]
) -> list[dict]:
    """네이버 블로그 최근 글(제목·발췌 400자·게시 시각). 한국어 블로그라 KR 발굴에만 싣는다."""
    channels = getattr(sources_cfg, "channels", None)
    blogs = list(getattr(channels, "naver_blog", None) or [])
    if market != "KR" or http is None or not blogs:
        return []
    try:
        from trader.sources.naver_blog import NaverBlogSource

        r = NaverBlogSource(
            blogs, http=http, max_posts=getattr(channels, "naver_blog_posts", 5), now=now
        ).fetch(asof=asof)
    except Exception as e:  # noqa: BLE001 - 선택 입력
        logger.debug("블로그 발굴 입력 실패: %s", e)
        return []
    out: list[dict] = []
    for label, items in (r.get("sources") or {}).items():
        for it in items:
            out.append(
                {
                    "source": label,
                    "title": it.get("title"),
                    "excerpt": (it.get("body") or "")[:400],
                    "posted_at": it.get("posted_at"),
                }
            )
    return out


def _check_us(market_client: Any, symbol: str) -> str | None:
    """시세가 오는 거래소 코드(NAS→NYS→AMS). 없으면 None. 클라이언트가 없으면 None."""
    if market_client is None:
        return None
    for exch in US_EXCHANGES:
        try:
            q = market_client.get_price_us(symbol, exch)
        except Exception:  # noqa: BLE001 - 다른 거래소에서 다시
            continue
        if q is not None and getattr(q, "price", None):
            return exch
    return None


def _check_kr(market_client: Any, code: str) -> bool:
    if market_client is None:
        return True  # 캐시 일치만으로 받는다
    try:
        q = market_client.get_price_kr(code)
    except Exception:  # noqa: BLE001
        return False
    return bool(q is not None and getattr(q, "price", None))


def resolve(
    discovered: dict,
    *,
    market: str,
    universe: Universe,
    cache_names: dict[tuple[str, str], str],
    market_client: Any | None = None,
    exclude: set[str] | None = None,
    max_listed: int = MAX_LISTED,
    event_watch: list[dict] | None = None,
    max_event: int = 0,
    max_per_event: int = 2,
) -> dict:
    """scout 출력(Discovered)을 파이썬이 확정한다 — 유니버스 종목은 코드 일치(이름으로도 찾음),
    밖이면 캐시 역색인으로 코드를 찾고 시세로 거래소를 확인한다. 못 찾으면 `unresolved`.
    tier=event(사건 수혜)는 입력 `event_watch` 의 종목만 받고(표가 코드의 출처) 거래소는 표 값,
    사건마다 max_per_event·합계 max_event 까지. 유니버스 종목이면 tier 는 universe 로 두고
    `event_id` 만 붙인다."""
    from trader.scan import events as events_mod

    ev_idx = events_mod.watch_index(event_watch or [])
    event_n = 0
    per_event: dict[str, int] = {}
    uni_codes = set(universe.kr) if market == "KR" else {u.symbol for u in universe.us}
    uni_exch = {u.symbol: u.exchange for u in universe.us} if market == "US" else {}
    uni_by_name = {
        normalize(cache_names.get((market, t), "")): t
        for t in uni_codes
        if cache_names.get((market, t))
    }
    rindex = reverse_index(cache_names, market)
    excl = set(exclude or ()) | set(universe.exclude)
    out: list[dict] = []
    unresolved: list[dict] = []
    seen: set[str] = set()
    listed_n = 0
    for c in discovered.get("candidates") or []:
        name = str(c.get("name") or "").strip()
        ticker = str(c.get("ticker") or "").strip().upper() if c.get("ticker") else None
        key = normalize(name)
        code: str | None = None
        tier = "universe"
        event = None
        if c.get("tier") == "event":
            event = ev_idx.get(ticker or "")
            if event is None:
                unresolved.append(
                    {"name": name, "ticker": ticker, "reason": "사건 수혜 표(event_watch)에 없음"}
                )
                continue
            if per_event.get(event["event_id"], 0) >= max_per_event:
                unresolved.append(
                    {"name": name, "ticker": ticker, "reason": f"사건당 상한 {max_per_event}"}
                )
                continue
        if event is not None and ticker not in uni_codes:
            code = ticker
            if code in excl or code in seen:
                unresolved.append({"name": name, "ticker": code, "reason": "제외·중복"})
                continue
            if event_n >= max_event:
                unresolved.append(
                    {"name": name, "ticker": code, "reason": f"사건 후보 상한 {max_event}"}
                )
                continue
            exchange = event.get("exchange") if market == "US" else None
            if market == "KR" and not _check_kr(market_client, code):
                unresolved.append({"name": name, "ticker": code, "reason": "KIS 시세 없음"})
                continue
            event_n += 1
            per_event[event["event_id"]] = per_event.get(event["event_id"], 0) + 1
            seen.add(code)
            out.append(
                {
                    "ticker": code,
                    "name": name or event.get("name") or "",
                    "market": market,
                    "exchange": exchange,
                    "tier": "event",
                    "event_id": event["event_id"],
                    "catalyst": str(c.get("catalyst") or "")[:200],
                    "confidence": float(c.get("confidence") or 0),
                    "source_ids": list(c.get("source_ids") or []),
                }
            )
            continue
        if ticker and ticker in uni_codes:
            code = ticker
        elif key and key in uni_by_name:
            code = uni_by_name[key]
        else:
            tier = "listed"
            code = ticker or rindex.get(key)
            if not code:
                unresolved.append({"name": name, "reason": "상장 종목 코드를 찾지 못함"})
                continue
            if market == "KR" and not _KR_CODE.match(code):
                unresolved.append({"name": name, "reason": f"KR 코드 형식 아님: {code}"})
                continue
            if market == "US" and not _US_SYM.match(code):
                unresolved.append({"name": name, "reason": f"US 심볼 형식 아님: {code}"})
                continue
        if code in excl:
            unresolved.append(
                {"name": name, "ticker": code, "reason": "제외 종목(헤지 ETF·exclude)"}
            )
            continue
        if code in seen:
            continue
        exchange = uni_exch.get(code) if market == "US" else None
        if tier == "listed":
            if listed_n >= max_listed:
                unresolved.append(
                    {"name": name, "ticker": code, "reason": f"유니버스 밖 상한 {max_listed}"}
                )
                continue
            if market == "US":
                exchange = _check_us(market_client, code)
                if exchange is None:
                    unresolved.append(
                        {"name": name, "ticker": code, "reason": "KIS 시세 없음(거래소 미확정)"}
                    )
                    continue
            elif not _check_kr(market_client, code):
                unresolved.append({"name": name, "ticker": code, "reason": "KIS 시세 없음"})
                continue
            listed_n += 1
        seen.add(code)
        if event is not None:
            per_event[event["event_id"]] = per_event.get(event["event_id"], 0) + 1
        out.append(
            {
                "ticker": code,
                "name": name or cache_names.get((market, code), ""),
                "market": market,
                "exchange": exchange,
                "tier": tier,
                **({"event_id": event["event_id"]} if event is not None else {}),
                "catalyst": str(c.get("catalyst") or "")[:200],
                "confidence": float(c.get("confidence") or 0),
                "source_ids": list(c.get("source_ids") or []),
            }
        )
    return {
        "candidates": out,
        "unresolved": unresolved,
        "notes": list(discovered.get("notes") or []),
    }


def merge(
    quant_candidates: list[dict],
    discovered: dict,
    *,
    max_listed: int = MAX_LISTED,
    max_event: int = 8,
) -> list[dict]:
    """screener 입력 순서: ① 유니버스∧발굴(정량 점수순) ② 유니버스 밖 발굴(≤max_listed, 확신순,
    score None) ②' 사건 수혜(유니버스 밖, ≤max_event) ③ 나머지 정량 후보. 발굴 후보에는
    `discovery: {tier, catalyst, confidence}`."""
    disc = {c["ticker"]: c for c in discovered.get("candidates") or []}
    by_ticker = {c["ticker"]: dict(c) for c in quant_candidates}
    first: list[dict] = []
    rest: list[dict] = []
    for c in quant_candidates:
        c = dict(c)
        d = disc.get(c["ticker"])
        if d and d.get("tier") == "universe":
            c["discovery"] = {k: d.get(k) for k in ("tier", "catalyst", "confidence")}
            c["reasons"] = [*(c.get("reasons") or []), f"뉴스 발굴: {d.get('catalyst', '')}"[:200]]
            first.append(c)
        else:
            rest.append(c)
    first.sort(key=lambda c: (c.get("score") is None, -(c.get("score") or 0)))
    listed = [
        d for d in disc.values() if d.get("tier") == "listed" and d["ticker"] not in by_ticker
    ]
    listed.sort(key=lambda d: -float(d.get("confidence") or 0))
    second = [
        {
            "ticker": d["ticker"],
            "exchange": d.get("exchange"),
            "score": None,
            "reasons": [f"뉴스 발굴(유니버스 밖): {d.get('catalyst', '')}"[:200]],
            "discovery": {k: d.get(k) for k in ("tier", "catalyst", "confidence")},
        }
        for d in listed[:max_listed]
    ]
    evs = [d for d in disc.values() if d.get("tier") == "event" and d["ticker"] not in by_ticker]
    evs.sort(key=lambda d: -float(d.get("confidence") or 0))
    third = [
        {
            "ticker": d["ticker"],
            "exchange": d.get("exchange"),
            "score": None,
            "reasons": [f"사건 수혜: {d.get('catalyst', '')}"[:200]],
            "discovery": {k: d.get(k) for k in ("tier", "catalyst", "confidence", "event_id")},
        }
        for d in evs[:max_event]
    ]
    return [*first, *second, *third, *rest]


def _event_watch(conn, market: str, today, asof) -> dict:
    """사건 수혜(2026-10-27): 지금 감지된 사건과 그 수혜 묶음(`config/events.yaml`). 꺼졌거나 표가
    없으면 빈 목록. 감지 결과는 `control.events:<시장>` 에도 남긴다(웹·게이트)."""
    from trader.config import load_events
    from trader.scan import events

    try:
        cfg = load_events()
    except Exception as e:  # noqa: BLE001 - 표 오류는 사건 발굴만 빠진다
        logger.warning("events.yaml 을 읽지 못해 사건 발굴 생략: %s", e)
        return {"event_watch": [], "max_event_picks": 0, "max_picks_per_event": 0}
    found = events.detect(conn, cfg, market=market, asof=asof) if conn is not None else []
    if conn is not None and cfg.enabled:
        events.record(conn, market, today, found)
    return {
        "event_watch": found,
        "max_event_picks": cfg.max_event_picks if cfg.enabled else 0,
        "max_picks_per_event": cfg.max_picks_per_event,
    }


def _theme_heat(conn, settings, market: str, today) -> list[dict]:
    """오늘 테마 열기 상위 top_n (Phase 25, 데몬 <시장>_theme). 없거나 다른 날이면 []. scout 는
    `catalyst_ok` 인 상위 테마의 대표 종목 1~2개를 후보로 올릴 수 있다(규칙 1 완화)."""
    from trader.control import control
    from trader.scan import theme

    if conn is None or not settings.theme.enabled:
        return []
    heat = control.get_flag(conn, f"{theme.FLAG}:{market}") or {}
    if heat.get("date") != today.isoformat():
        return []
    return [
        {
            "theme": t["theme"],
            "rank": t["rank"],
            "score": t["score"],
            "catalyst_ok": t.get("catalyst_ok", False),
            "catalyst_ids": t.get("catalyst_ids") or [],
            "tickers": t.get("tickers") or [],
        }
        for t in theme.top(heat, settings.theme.top_n)
    ]


def summary(discovery: dict | None) -> dict | None:
    """`_scan.json.discovery`: 발굴 요약 (후보 코드·tier, 미해결 수)."""
    if not discovery:
        return None
    cands = discovery.get("candidates") or []
    return {
        "candidates": [
            {k: c.get(k) for k in ("ticker", "name", "tier", "exchange", "catalyst", "confidence")}
            for c in cands
        ],
        "universe": sum(1 for c in cands if c.get("tier") == "universe"),
        "listed": sum(1 for c in cands if c.get("tier") == "listed"),
        "unresolved": len(discovery.get("unresolved") or []),
    }


def line(discovery: dict | None) -> str:
    """알림·STATUS 한 줄: '뉴스 발굴 3(유니버스 2·밖 1·미해결 1)'."""
    s = summary(discovery)
    if not s:
        return "뉴스 발굴 없음"
    n = len(s["candidates"])
    return f"뉴스 발굴 {n}(유니버스 {s['universe']}·밖 {s['listed']}·미해결 {s['unresolved']})"
