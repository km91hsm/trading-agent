"""종목의 오늘 상위 테마·shock 사건 소속 조회(2026-10-28 결정 19번) — 게이트(`executor._theme_of`)와
트레이더 입력(`tools` 묶음 `context.theme`)이 같은 함수를 쓴다.

기록(`control.theme_heat:<시장>`·`control.events:<시장>`)은 그날 최신값 하나라 과거 시각으로
되돌릴 수 없다. 그래서 `asof`(입력 기준 시각)를 주면 관측 시각이 그 뒤인 기록은 넣지 않고
(`after_asof`), 관측 시각이 없는 옛 기록은 시각을 몰라 넣지 않는다(`unknown_time`) — 오래된
`00_inputs` 에 그날 나중에 감지된 테마를 붙이는 룩어헤드를 막는다. `asof` 가 없으면(실행 게이트)
오늘 최신값을 쓴다.
상태: ok | none(오늘 기록 없음·소속 없음) | off(꺼짐) | after_asof | unknown_time | error.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime

logger = logging.getLogger("trader.scan")


def _time_ok(observed: str | None, asof: datetime | None) -> str:
    if asof is None:
        return "ok"
    if not observed:
        return "unknown_time"
    try:
        return "ok" if datetime.fromisoformat(observed) <= asof else "after_asof"
    except ValueError:
        return "unknown_time"


def of(
    conn: sqlite3.Connection,
    market: str,
    d: date,
    *,
    theme_top_n: int,
    theme_enabled: bool,
    events_enabled: bool,
    asof: datetime | None = None,
) -> dict:
    """{map: {종목: [테마…, '사건:<label>'…]}, status: {theme, events},
    observed_at: {theme, events}}."""
    from trader.control import control
    from trader.scan import events, theme

    merged: dict[str, list[str]] = {}
    status = {"theme": "off", "events": "off"}
    observed: dict[str, str | None] = {"theme": None, "events": None}
    if theme_enabled:
        try:
            heat = control.get_flag(conn, f"{theme.FLAG}:{market}") or {}
            if heat.get("date") != d.isoformat():
                status["theme"] = "none"
            else:
                observed["theme"] = heat.get("observed_at")
                status["theme"] = _time_ok(observed["theme"], asof)
                if status["theme"] == "ok":  # 검증한 그 기록에서 뽑는다(다시 읽지 않음)
                    for t, ths in theme.members_of(heat, theme_top_n).items():
                        merged.setdefault(t, []).extend(ths)
        except Exception as e:  # noqa: BLE001 - 그 규칙만 빠진다
            logger.warning("테마 맵 읽기 실패: %s", e)
            status["theme"] = "error"
    if events_enabled:
        try:
            st = control.get_flag(conn, f"events:{market}") or {}
            if st.get("date") != d.isoformat():
                status["events"] = "none"
            else:
                observed["events"] = st.get("observed_at")
                status["events"] = _time_ok(observed["events"], asof)
                if status["events"] == "ok":  # 검증한 그 기록에서 뽑는다(다시 읽지 않음)
                    for t, evs in events.shock_members(st.get("events") or []).items():
                        merged.setdefault(t, []).extend(evs)
        except Exception as e:  # noqa: BLE001
            logger.warning("사건 묶음 읽기 실패: %s", e)
            status["events"] = "error"
    return {"map": merged, "status": status, "observed_at": observed}


def for_ticker(result: dict, ticker: str) -> dict:
    """트레이더 입력용 한 종목 요약 {themes, events, status, observed_at}."""
    labels = list((result.get("map") or {}).get(ticker) or [])
    return {
        "themes": [x for x in labels if not x.startswith("사건:")],
        "events": [x.removeprefix("사건:") for x in labels if x.startswith("사건:")],
        "status": result.get("status"),
        "observed_at": result.get("observed_at"),
    }
