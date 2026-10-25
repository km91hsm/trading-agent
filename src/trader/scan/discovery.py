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
) -> dict:
    """scout 입력을 조립해 `_discovery_inputs` 로 저장하고 돌려준다. 뉴스가 비어도 파일은 만든다
    (scout 이 '발굴 없음' 을 낼 수 있게)."""
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
    news = news_store.recent(conn, market, asof=asof, hours=cfg.store_hours, limit=cfg.store_items)
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
                k: snap.get(k)
                for k in ("observed_at", "trends", "movers", "community_top5", "report")
            }
    except Exception as e:  # noqa: BLE001 - 선택 입력
        logger.debug("세이브티커 발굴 입력 실패 %s: %s", market, e)
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
        "recent_picks": _recent_picks(market, today, state),
        "saveticker": saveticker_snap,
        "quant_candidates": [
            {k: c.get(k) for k in ("ticker", "exchange", "score", "reasons")}
            for c in quant_candidates[:30]
        ],
        "max_listed": MAX_LISTED,
    }
    d = market_dir(today, market, state, create=True)
    write_checkpoint(d, "_discovery_inputs", {"schema_version": 1, "data": data, "call": None})
    return data


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
) -> dict:
    """scout 출력(Discovered)을 파이썬이 확정한다 — 유니버스 종목은 코드 일치(이름으로도 찾음),
    밖이면 캐시 역색인으로 코드를 찾고 시세로 거래소를 확인한다. 못 찾으면 `unresolved`."""
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
        out.append(
            {
                "ticker": code,
                "name": name or cache_names.get((market, code), ""),
                "market": market,
                "exchange": exchange,
                "tier": tier,
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
    quant_candidates: list[dict], discovered: dict, *, max_listed: int = MAX_LISTED
) -> list[dict]:
    """screener 입력 순서: ① 유니버스∧발굴(정량 점수순) ② 유니버스 밖 발굴(≤max_listed, 확신순,
    score None) ③ 나머지 정량 후보. 발굴 후보에는 `discovery: {tier, catalyst, confidence}`."""
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
    return [*first, *second, *rest]


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
