"""정규장 열림 판정 (Phase 24 웹 실시간 시세). KR 09:00~15:30 KST, US 09:30~16:00 ET, 평일만.

US 휴장은 `daemon.us_holidays`(NYSE 규칙)를 쓰고, KR 휴장은 KIS 조회 없이는 모르므로 평일이면 연다
(휴장일 시세 조회는 전일 종가가 돌아올 뿐 해가 없다)."""

from __future__ import annotations

from datetime import datetime

from trader.config import MARKET_TZ

REGULAR = {"KR": ("09:00", "15:30"), "US": ("09:30", "16:00")}


def is_open(market: str, now: datetime) -> bool:
    from trader.daemon import in_window, us_holidays

    local = now.astimezone(MARKET_TZ[market])
    if local.weekday() >= 5:
        return False
    if market == "US" and local.date() in us_holidays(local.year):
        return False
    start, end = REGULAR[market]
    return in_window(local.time().replace(second=0, microsecond=0), start, end)
