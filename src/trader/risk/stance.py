"""성향 전환 (스펙 §6.1): 최근 5거래일 실현손익이 음수면 conservative."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from trader.config import RiskLimits

Stance = Literal["normal", "conservative"]

CONSERVATIVE_POSITION_PCT = 0.15
CONSERVATIVE_MAX_POSITIONS = 2
NEUTRAL_REGIME_MAX_POSITIONS = 3  # 국면 neutral: 종목당 15%·3종목 (Phase 17 H)
# Phase 22 전략가 기울기(agent tilt): 시장 세션 RISK_ON(+1)/RISK_OFF(−1) 가 국면 한도를 한 칸 옮긴다
TILT_UP_POSITION_PCT = 0.20  # neutral + RISK_ON
TILT_UP_MAX_POSITIONS = 4
TILT_DOWN_POSITION_PCT = 0.10  # neutral + RISK_OFF, bear + RISK_ON(조건부 허용)
TILT_DOWN_MAX_POSITIONS = 2


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


def _cap(limits: RiskLimits, pct: float, n: int) -> RiskLimits:
    return limits.model_copy(
        update={
            "max_position_pct": min(limits.max_position_pct, pct),
            "max_positions": min(limits.max_positions, n),
        }
    )


def limits_for_regime(regime: str, limits: RiskLimits, tilt: int = 0) -> RiskLimits:
    """국면별 신규 매수 한도. bull 은 그대로, neutral 은 종목당 15%·3종목. bear 는 한도가 아니라
    거부 규칙(rules.evaluate)이 신규 매수를 막는다.

    tilt(Phase 22, 전략가 기울기 +1 RISK_ON / −1 RISK_OFF / 0)는 한 칸만 옮긴다: bull−1 → neutral
    한도, neutral+1 → 20%·4, neutral−1 → 10%·2, bear+1 → 10%·2(거부 규칙이 조건부로 연다).
    설정 한도보다 넓어지지는 않는다(min)."""
    if regime == "bull":
        return (
            _cap(limits, CONSERVATIVE_POSITION_PCT, NEUTRAL_REGIME_MAX_POSITIONS)
            if tilt < 0
            else limits
        )
    if regime == "neutral":
        if tilt > 0:
            return _cap(limits, TILT_UP_POSITION_PCT, TILT_UP_MAX_POSITIONS)
        if tilt < 0:
            return _cap(limits, TILT_DOWN_POSITION_PCT, TILT_DOWN_MAX_POSITIONS)
        return _cap(limits, CONSERVATIVE_POSITION_PCT, NEUTRAL_REGIME_MAX_POSITIONS)
    if regime == "bear" and tilt > 0:
        return _cap(limits, TILT_DOWN_POSITION_PCT, TILT_DOWN_MAX_POSITIONS)
    return limits


def tilt_of(view: dict | None, *, min_confidence: float) -> int:
    """오늘 시장 세션 제안 → 기울기. RISK_ON +1, RISK_OFF −1, 그 외·확신 부족·없음 0."""
    if not view:
        return 0
    try:
        conf = float(view.get("confidence") or 0)
    except (TypeError, ValueError):
        return 0
    if conf < min_confidence:
        return 0
    stance = view.get("stance")
    return 1 if stance == "RISK_ON" else -1 if stance == "RISK_OFF" else 0
