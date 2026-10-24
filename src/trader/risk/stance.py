"""성향 전환 (스펙 §6.1): 최근 5거래일 실현손익이 음수면 conservative."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from trader.config import RiskLimits

Stance = Literal["normal", "conservative"]

CONSERVATIVE_POSITION_PCT = 0.15
CONSERVATIVE_MAX_POSITIONS = 2
NEUTRAL_REGIME_MAX_POSITIONS = 3  # 국면 neutral: 종목당 15%·3종목 (Phase 17 H)


def decide(realized_pnl_5d: Decimal) -> Stance:
    return "conservative" if realized_pnl_5d < 0 else "normal"


def limits_for(stance: Stance, limits: RiskLimits) -> RiskLimits:
    """성향에 맞춘 한도. normal 은 그대로, conservative 는 종목당 15%·신규 2종목으로 조인다."""
    if stance == "normal":
        return limits
    return limits.model_copy(
        update={
            "max_position_pct": min(limits.max_position_pct, CONSERVATIVE_POSITION_PCT),
            "max_positions": min(limits.max_positions, CONSERVATIVE_MAX_POSITIONS),
        }
    )


def limits_for_regime(regime: str, limits: RiskLimits) -> RiskLimits:
    """국면 neutral 이면 신규 매수 한도를 종목당 15%·3종목으로 조인다. bull 은 그대로.
    bear 는 한도가 아니라 거부 규칙(rules.evaluate)이 신규 매수를 막는다."""
    if regime != "neutral":
        return limits
    return limits.model_copy(
        update={
            "max_position_pct": min(limits.max_position_pct, CONSERVATIVE_POSITION_PCT),
            "max_positions": min(limits.max_positions, NEUTRAL_REGIME_MAX_POSITIONS),
        }
    )
