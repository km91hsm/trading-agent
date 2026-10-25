"""보유기간 프로필(Phase 23 T): 결정의 horizon_days → short|mid|long → 장중 점검 주기·임계.

판단 주기는 층마다 다르다 — 초~분: 파이썬 monitor(손절), 분~시간: 트리거·뉴스 재평가 세션,
일: 아침 scan→analyze, 중·장기: 만기 복기. 이 모듈은 그 층의 **임계·간격**을 결정의 보유기간으로
차등하는 순수 함수만 둔다(설정 원본은 `RiskLimits.horizon_profiles`)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from trader.config import HorizonProfile, RiskLimits

SHORT_MAX_DAYS = 10
MID_MAX_DAYS = 40
PERIODIC_DAILY_CAP = 6  # 정기 재평가 운영일 상한(종목당)
LABEL = {"short": "단기", "mid": "중기", "long": "장기"}


def name_of(horizon_days: int | None) -> str:
    """horizon_days → 프로필 이름. 없거나 이상한 값은 mid."""
    try:
        h = int(horizon_days) if horizon_days is not None else None
    except (TypeError, ValueError):
        h = None
    if h is None or h <= 0:
        return "mid"
    if h <= SHORT_MAX_DAYS:
        return "short"
    if h <= MID_MAX_DAYS:
        return "mid"
    return "long"


def profile_of(limits: RiskLimits, horizon_days: int | None) -> HorizonProfile:
    return limits.horizon_profiles[name_of(horizon_days)]


def of_decision(limits: RiskLimits, decision: dict[str, Any] | None) -> tuple[str, HorizonProfile]:
    """결정 행(`repo.latest_decision` 의 data 또는 Decision dump)에서 (이름, 프로필)."""
    h = (decision or {}).get("horizon_days")
    name = name_of(h)
    return name, limits.horizon_profiles[name]


def trading_days_between(start: date, end: date) -> int:
    """start 다음 날부터 end 까지의 평일 수(휴장일은 보지 않는다 — 재분석 주기용 근사)."""
    n, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def reanalyze_due(profile: HorizonProfile, last_decision_date: date | None, today: date) -> bool:
    """보유 종목의 아침 재분석이 오늘 필요한가. 결정이 없으면 항상, 있으면 마지막 결정 뒤 평일 수가
    `daily_reanalyze_every` 이상일 때."""
    if last_decision_date is None or last_decision_date >= today:
        return last_decision_date is None
    return trading_days_between(last_decision_date, today) >= profile.daily_reanalyze_every
