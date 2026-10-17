"""거래 비용과 기대수익 (스펙 §6.1): KR 왕복 30bp, US 50bp."""

from __future__ import annotations

from decimal import Decimal

from trader.agents.schemas import Decision

_ROUND_TRIP_BP = {"KR": 30, "US": 50}


def round_trip_bp(market: str) -> int:
    """시장별 왕복 비용(bp). 모르는 시장은 보수적으로 US 값."""
    return _ROUND_TRIP_BP.get(market, _ROUND_TRIP_BP["US"])


def expected_return(decision: Decision, price: Decimal) -> float | None:
    """현재가 대비 take_profit 까지의 기대수익률. take_profit 이 없으면 None(판단 불가)."""
    if decision.take_profit is None or price <= 0:
        return None
    return float(Decimal(str(decision.take_profit)) / price - 1)
