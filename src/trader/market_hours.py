"""정규장 열림 판정 (Phase 24 웹 실시간 시세). KR 09:00~15:30 KST, US 09:30~16:00 ET, 평일만.

US 휴장은 `daemon.us_holidays`(NYSE 규칙)를 쓰고, KR 휴장은 KIS 조회 없이는 모르므로 평일이면 연다
(휴장일 시세 조회는 전일 종가가 돌아올 뿐 해가 없다)."""

from __future__ import annotations

from datetime import date, datetime

from trader.config import MARKET_TZ

REGULAR = {"KR": ("09:00", "15:30"), "US": ("09:30", "16:00")}


US_EARLY_CLOSE = "13:00"  # NYSE 조기 마감일(추수감사절 다음 날 등, daemon.us_early_closes)


def close_hm(market: str, day: date) -> str:
    """그날 정규장 마감 시각(현지 'HH:MM'). US 조기 마감일은 13:00 — 장중 잡·장 상태가 이걸 본다
    (조기 마감 뒤에도 monitor·단타·뉴스 재분석이 돌던 문제, 전체 검토 2026-10-27)."""
    if market == "US":
        from trader.daemon import us_early_closes

        if day in us_early_closes(day.year):
            return US_EARLY_CLOSE
    return REGULAR[market][1]


def _no_session(market: str, local_day: date) -> bool:
    from trader.daemon import us_holidays

    if local_day.weekday() >= 5:
        return True
    return market == "US" and local_day in us_holidays(local_day.year)


def is_open(market: str, now: datetime) -> bool:
    from trader.daemon import in_window

    local = now.astimezone(MARKET_TZ[market])
    if _no_session(market, local.date()):
        return False
    start = REGULAR[market][0]
    return in_window(
        local.time().replace(second=0, microsecond=0), start, close_hm(market, local.date())
    )


def session_over(market: str, now: datetime) -> bool:
    """그 시장 현지 오늘 정규장이 이미 끝났거나(조기 마감 포함) 장이 없는 날(주말·US 휴장). KR
    휴장일은 KIS 조회가 필요해 여기서는 모른다 — 호출자가 `daemon.should_skip` 을 함께 본다."""
    local = now.astimezone(MARKET_TZ[market])
    if _no_session(market, local.date()):
        return True
    hm = local.strftime("%H:%M")
    return hm >= close_hm(market, local.date())
