"""사건 수혜(2026-10-27): 뉴스 저장소에서 사건 키워드를 세어 '지금 일어난 사건' 을 파이썬이
감지하고, `config/events.yaml` 의 수혜 묶음을 scout 입력(`event_watch`)으로 넘긴다.

- 감지는 파이썬(키워드 건수 ≥ min_news), 종목은 표가 출처 — LLM 은 사건이 실제로 진행 중인지 보고
  표 안에서 1~2개를 고를 뿐이다(종목 코드를 지어내지 않게).
- 결과는 `control.events:<시장>` = {date, events:[…]} 에 남아 웹·리스크 게이트가 읽는다.
- kind=shock 은 테마 안전장치(같은 사건 묶음 비중 상한·단기 보유·익절 필수, `risk/rules`)를 받는다.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime

from trader.config import EventsConfig
from trader.control import control

logger = logging.getLogger("trader.scan")

FLAG = "events"
ALERTED = "events_alerted"
NEWS_LIMIT = 400  # 감지에 읽는 최근 뉴스 상한(시장별)
SHOW_NEWS = 5  # 사건마다 근거로 넘기는 뉴스 수


def _flag(market: str) -> str:
    return f"{FLAG}:{market}"


def _matches(text: str, keywords: list[str]) -> bool:
    low = text.casefold()
    return any(k.casefold() in low for k in keywords)


def detect(
    conn: sqlite3.Connection, cfg: EventsConfig, *, market: str, asof: datetime
) -> list[dict]:
    """최근 lookback_hours 뉴스에서 사건별 키워드 건수를 세어 min_news 이상인 사건(그 시장 수혜
    종목이 있는 것만)을 건수 순으로. 항목은 {id, label, kind, hits, news:[{id,title}], tickers}."""
    if not cfg.enabled or not cfg.events:
        return []
    from trader.sources import news_store

    items = news_store.recent(conn, market, asof=asof, hours=cfg.lookback_hours, limit=NEWS_LIMIT)
    out: list[dict] = []
    for ev in cfg.events:
        tickers = ev.beneficiaries.get(market) or []  # type: ignore[call-overload]
        if not tickers:
            continue
        hits = [
            n
            for n in items
            if _matches(f"{n.get('title') or ''} {n.get('summary') or ''}", ev.keywords)
        ]
        if len(hits) < ev.min_news:
            continue
        out.append(
            {
                "id": ev.id,
                "label": ev.label,
                "kind": ev.kind,
                "hits": len(hits),
                "news": [
                    {"id": n.get("id"), "title": str(n.get("title") or "")[:120]}
                    for n in hits[:SHOW_NEWS]
                ],
                "tickers": [t.model_dump(exclude_none=True) for t in tickers],
            }
        )
    out.sort(key=lambda e: -e["hits"])
    return out


def record(conn: sqlite3.Connection, market: str, today: date, events: list[dict]) -> None:
    """오늘 감지 결과를 남긴다(웹 표시·게이트의 사건 묶음). 같으면 쓰지 않는다."""
    value = {"date": today.isoformat(), "events": events}
    if control.get_flag(conn, _flag(market)) != value:
        control.set_flag(conn, _flag(market), value)


def today_events(conn: sqlite3.Connection, market: str, today: date) -> list[dict]:
    st = control.get_flag(conn, _flag(market)) or {}
    return list(st.get("events") or []) if st.get("date") == today.isoformat() else []


def theme_of_map(conn: sqlite3.Connection, market: str, today: date) -> dict[str, list[str]]:
    """오늘 감지된 **shock** 사건의 수혜 종목 → ['사건:<label>'] (테마 안전장치에 합친다).
    structural(몇 달 가는 흐름)은 일반 규칙만 — 단기 보유를 강제하지 않는다."""
    out: dict[str, list[str]] = {}
    for ev in today_events(conn, market, today):
        if ev.get("kind") != "shock":
            continue
        for t in ev.get("tickers") or []:
            out.setdefault(str(t["ticker"]), []).append(f"사건:{ev['label']}")
    return out


def watch_index(event_watch: list[dict]) -> dict[str, dict]:
    """scout 입력의 event_watch → 종목 → {event_id, label, exchange, name} (resolve 가 검증에 쓴다).
    여러 사건에 걸친 종목은 건수가 많은 사건(앞쪽)을 쓴다."""
    idx: dict[str, dict] = {}
    for ev in event_watch or []:
        for t in ev.get("tickers") or []:
            idx.setdefault(
                str(t["ticker"]).upper(),
                {
                    "event_id": ev["id"],
                    "label": ev["label"],
                    "exchange": t.get("exchange"),
                    "name": t.get("name"),
                },
            )
    return idx


def new_for_alert(
    conn: sqlite3.Connection, market: str, today: date, events: list[dict]
) -> list[dict]:
    """오늘 아직 알리지 않은 사건만 돌려주고 알린 것으로 적는다(시장·운영일 1회)."""
    key = f"{ALERTED}:{market}:{today.isoformat()}"
    done = set(control.get_flag(conn, key) or [])
    fresh = [e for e in events if e["id"] not in done]
    if fresh:
        control.set_flag(conn, key, sorted(done | {e["id"] for e in fresh}))
    return fresh
