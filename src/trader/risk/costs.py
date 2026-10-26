"""거래 비용과 기대수익 (스펙 §6.1): KR 왕복 30bp, US 50bp."""

from __future__ import annotations

from decimal import Decimal

from trader.agents.schemas import Decision

_ROUND_TRIP_BP = {"KR": 30, "US": 50}


def round_trip_bp(market: str) -> int:
    """시장별 왕복 비용(bp). 모르는 시장은 보수적으로 US 값."""
    return _ROUND_TRIP_BP.get(market, _ROUND_TRIP_BP["US"])


def expected_value(decision: Decision, price: Decimal) -> float | None:
    """확신 가중 기대값(가격 대비): p·(익절 상승률) − (1−p)·(손절 하락률), p = confidence(0.05~0.95
    로 자름). 익절·손절 중 하나라도 없으면 None. 목표 상승률만 보던 게이트는 손절 거리와 확률을
    무시해 음의 기대값(목표 +2%·손절 −20%)도 통과시켰다(전체 검토 L9)."""
    if decision.take_profit is None or decision.stop_loss is None or price <= 0:
        return None
    p = min(max(float(decision.confidence), 0.05), 0.95)
    up = float(Decimal(str(decision.take_profit)) / price - 1)
    down = float(1 - Decimal(str(decision.stop_loss)) / price)
    return p * up - (1 - p) * down


def expected_return(decision: Decision, price: Decimal) -> float | None:
    """현재가 대비 take_profit 까지의 기대수익률. take_profit 이 없으면 None(판단 불가)."""
    if decision.take_profit is None or price <= 0:
        return None
    return float(Decimal(str(decision.take_profit)) / price - 1)
