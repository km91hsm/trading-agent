"""성향 전환 (스펙 §6.1): 최근 5거래일 실현손익이 음수면 conservative."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from trader.config import RiskLimits

Stance = Literal["normal", "conservative"]

CONSERVATIVE_POSITION_PCT = 0.15
CONSERVATIVE_MAX_POSITIONS = 2


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
