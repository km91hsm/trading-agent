"""파생 결정 스키마(Phase 26, 스펙 §3). 정본은 여기이고 단계 3 의 `agents/schemas.py` 가
re-export 해 프롬프트에 붙인다. 용어(§0): `direction` 은 시장 방향 뷰(LONG|SHORT|FLAT),
`vehicle` 은 수단, `contract_side` 는 계약 보유 방향(매수 LONG·매도 SHORT) — 혼용하지 않는다."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trader.broker.models import ContractSide, FoKind

FoIntent = Literal["OPEN", "RESIZE", "HOLD", "CLOSE", "REPLACE"]
Direction = Literal["LONG", "SHORT", "FLAT"]
Vehicle = Literal["FUTURES", "OPTION_BUY", "OPTION_SELL"]
StrikeRule = Literal["ATM", "OTM1", "OTM2", "ITM1"]
ENTRY_DECISIONS = ("OPEN", "RESIZE", "REPLACE")


class FoInvalidationRule(BaseModel):
    """장중 모니터가 검사하는 파생 무효화 조건 — 현물 `InvalidationRule`(L16) 의 `price` 형식과
    같고 값은 **기초지수(코스피200) 레벨**. 모니터가 지수 완성 일봉을 들고 있지 않아 `close` 는
    없다. 판정은 `orchestrator.monitor.rule_hit` 하나(현물과 같은 함수)."""

    model_config = ConfigDict(extra="forbid")

    metric: Literal["price"] = "price"
    op: Literal["<", "<=", ">", ">="]
    value: float = Field(gt=0)

    @field_validator("value")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("value 는 유한한 양수여야 합니다")
        return v


class FoDecision(BaseModel):
    """fo-trader 의 결정 1건. OPEN/RESIZE/REPLACE 는 `stop_pt`(기초지수 레벨) 필수, FLAT 은
    HOLD 또는 CLOSE 만. RESIZE 는 같은 계약의 **목표 총량**(배수로 표현), REPLACE 는 청산 완료 뒤
    새 OPEN(다음 틱 이후)."""

    model_config = ConfigDict(extra="forbid")

    intent: FoIntent
    direction: Direction
    vehicle: Vehicle
    target_leverage: float = Field(default=0.0, ge=0, le=5)
    strike_rule: StrikeRule = "ATM"
    expiry_rule: Literal["front", "next"] = "front"
    order_type: Literal["LIMIT", "MARKET"] = "LIMIT"
    entry_price: float | None = None
    stop_pt: float | None = None
    take_profit_pt: float | None = None
    premium_stop_mult: float | None = None
    horizon_days: int = Field(default=5, ge=1, le=120)
    confidence: float = Field(default=0.5, ge=0, le=1)
    rationale: str = Field(default="", max_length=500)
    invalidation_conditions: list[str] = Field(default_factory=list)  # 설명용(자동으로 읽지 않음)
    invalidation_rules: list[FoInvalidationRule] = Field(default_factory=list, max_length=5)
    vs_spot: str | None = None

    @model_validator(mode="after")
    def _consistency(self) -> FoDecision:
        if self.intent in ENTRY_DECISIONS:
            if self.stop_pt is None:
                raise ValueError(f"{self.intent} 에는 stop_pt(기초지수 손절선)가 필요합니다")
            if self.direction == "FLAT":
                raise ValueError("FLAT 은 HOLD 또는 CLOSE 만 가능합니다")
            if self.target_leverage <= 0:
                raise ValueError(f"{self.intent} 에는 양의 target_leverage 가 필요합니다")
        if self.direction == "FLAT" and self.intent not in ("HOLD", "CLOSE"):
            raise ValueError("FLAT 은 HOLD 또는 CLOSE 만 가능합니다")
        return self


class FoDecisionEnvelope(BaseModel):
    """결정 봉투(R13). 아침 결정은 `account_id=None`(계좌별 판정은 파이썬), 재평가는 (계좌, 포지션,
    expected_version) 단위로 큐 요청이 주고 세션이 그대로 되돌린다(불일치면 도구가 거부)."""

    model_config = ConfigDict(extra="forbid")

    decision: FoDecision
    request_id: str = Field(min_length=1)
    account_id: str | None = None
    position_id: int | None = None
    expected_version: int | None = None


def contract_side_of(direction: str, vehicle: str) -> ContractSide:
    """LONG+FUTURES=매수, SHORT+FUTURES=매도, OPTION_BUY 는 항상 매수(LONG), OPTION_SELL 은 매도."""
    if vehicle == "FUTURES":
        return "LONG" if direction == "LONG" else "SHORT"
    return "LONG" if vehicle == "OPTION_BUY" else "SHORT"


def kind_of(direction: str, vehicle: str) -> FoKind:
    """LONG+OPTION_BUY=콜 매수, LONG+OPTION_SELL=풋 매도, SHORT+OPTION_BUY=풋 매수,
    SHORT+OPTION_SELL=콜 매도."""
    if vehicle == "FUTURES":
        return "FUTURES"
    if vehicle == "OPTION_BUY":
        return "CALL" if direction == "LONG" else "PUT"
    return "PUT" if direction == "LONG" else "CALL"


def order_side(contract_side: str, *, entry: bool) -> Literal["BUY", "SELL"]:
    """진입은 contract_side 와 같은 쪽, 청산은 반대."""
    buy = (contract_side == "LONG") == entry
    return "BUY" if buy else "SELL"


def kind_sign(kind: str) -> int:
    """1차 민감도 부호: 선물·콜 +1, 풋 −1."""
    return -1 if kind == "PUT" else 1
