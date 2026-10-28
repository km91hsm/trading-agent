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
from datetime import UTC, date, datetime

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
    """한글 키워드는 부분 문자열, 영문(ASCII) 키워드는 단어 경계로 — `war` 가 software·award 에
    걸리지 않게(Codex 리뷰)."""
    import re

    low = text.casefold()
    for k in keywords:
        kk = k.casefold()
        if kk.isascii():
            if re.search(rf"(?<![a-z0-9]){re.escape(kk)}(?![a-z0-9])", low):
                return True
        elif kk in low:
            return True
    return False


def _hits(items: list[dict], keywords: list[str], exclude: list[str]) -> list[dict]:
    """키워드가 든 뉴스(최신순). 제외 패턴이 든 것은 세지 않고, 같은 기사가 매체마다 따로 적재된
    것(세이브티커 22스트림·네이버·구글이 같은 통신 기사를 각각)은 정규화 제목으로 1건만 센다 —
    기사 1건이 `min_news` 를 넘기던 것(2-L10)."""
    from trader.sources import news_store

    seen: set[str] = set()
    out: list[dict] = []
    for n in items:
        title = str(n.get("title") or "")
        text = f"{title} {n.get('summary') or ''}"
        if not _matches(text, keywords) or (exclude and _matches(text, exclude)):
            continue
        key = news_store.normalize_title(title) or f"id:{n.get('id')}"
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out


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
        hits = _hits(items, ev.keywords, ev.exclude)
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


def record(
    conn: sqlite3.Connection,
    market: str,
    today: date,
    events: list[dict],
    *,
    now: datetime | None = None,
) -> None:
    """오늘 감지 결과를 남긴다(웹 표시·게이트의 사건 묶음). 같으면 쓰지 않는다. `observed_at` 은
    이 결과를 처음 본 시각 — 트레이더 입력이 기준 시각 뒤 감지를 넣지 않게(결정 19번,
    `scan/membership`)."""
    prev = control.get_flag(conn, _flag(market)) or {}
    if prev.get("date") == today.isoformat() and prev.get("events") == events:
        return
    value = {
        "date": today.isoformat(),
        "events": events,
        "observed_at": (now or datetime.now(UTC)).isoformat(),
    }
    control.set_flag(conn, _flag(market), value)


def today_events(conn: sqlite3.Connection, market: str, today: date) -> list[dict]:
    st = control.get_flag(conn, _flag(market)) or {}
    return list(st.get("events") or []) if st.get("date") == today.isoformat() else []


def theme_of_map(conn: sqlite3.Connection, market: str, today: date) -> dict[str, list[str]]:
    """오늘 감지된 **shock** 사건의 수혜 종목 → ['사건:<label>'] (테마 안전장치에 합친다).
    structural(몇 달 가는 흐름)은 일반 규칙만 — 단기 보유를 강제하지 않는다."""
    return shock_members(today_events(conn, market, today))


def shock_members(evs: list[dict]) -> dict[str, list[str]]:
    """이미 읽은 사건 목록에서 shock 수혜 종목 → ['사건:<label>'](다시 읽지 않는다,
    PR #65 Codex)."""
    out: dict[str, list[str]] = {}
    for ev in evs or []:
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
