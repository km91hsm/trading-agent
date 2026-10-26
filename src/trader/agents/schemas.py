"""에이전트 출력 스키마 (스펙 §4.2~4.4). 체크포인트 이름 → 스키마 매핑 포함."""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Signal = Literal["STRONG_BUY", "BUY", "NEUTRAL", "SELL", "STRONG_SELL", "UNKNOWN"]
Horizon = Literal["short", "mid", "long"]
AnalystRole = Literal["technical", "fundamental", "news_macro", "sentiment", "quant"]
Side = Literal["bull", "bear"]
Stance = Literal["BULLISH", "NEUTRAL", "BEARISH"]
ValuationView = Literal["UNDERVALUED", "FAIR", "OVERVALUED", "UNKNOWN"]
TimingAction = Literal["NOW", "CONDITIONAL", "WAIT"]
Action = Literal["BUY", "SELL", "HOLD", "ADD", "TRIM"]
OrderType = Literal["LIMIT", "MARKET"]

CHECKPOINT_SCHEMA_VERSION = 1


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: str
    source_id: str


class AnalystReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str
    asof: str
    role: AnalystRole
    summary: str = Field(max_length=300)
    bullets: list[str] = Field(max_length=7)
    signal: Signal
    score: float = Field(ge=-1, le=1)
    confidence: float = Field(ge=0, le=1)
    horizon: Horizon
    key_risks: list[str]
    counter_evidence: list[str]
    evidence: list[Evidence]
    data_gaps: list[str]


class Timing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: TimingAction
    condition: str | None = None
    price_zone: str | None = None


class DebateTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    side: Side
    round: int = Field(ge=0)
    stance: Stance
    valuation_view: ValuationView
    arguments: list[str] = Field(max_length=5)
    rebuttals: list[str] = Field(max_length=5)
    timing: Timing
    conviction: float = Field(ge=0, le=1)
    what_would_change_my_mind: list[str]


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str
    action: Action
    target_weight: float = Field(ge=0, le=0.25)
    order_type: OrderType
    limit_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    horizon_days: int = Field(ge=1)
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(max_length=500)
    invalidation_conditions: list[str]
    memory_ids_used: list[str] = Field(default_factory=list)


class QuantReport(BaseModel):
    """05_quant: 승격된 퀀트 모델의 horizon_days 뒤 수익률 3분류 확률 (파이썬 생성, 스펙 §9.1).

    label_mode 가 absolute 면 p_up = ±2% 기준 상승 확률, relative_tercile 이면 같은 날 시장 내 상위
    삼분위에 들 확률(시장 방향이 아니라 상대 순위)."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    asof: str
    role: Literal["quant"] = "quant"
    model: str
    p_up: float = Field(ge=0, le=1)
    p_flat: float = Field(ge=0, le=1)
    p_down: float = Field(ge=0, le=1)
    signal: Signal
    horizon_days: int = Field(ge=1)
    label_mode: Literal["absolute", "relative_tercile"] = "absolute"
    features_used: list[str]
    data_gaps: list[str] = Field(default_factory=list)
    feature_date: str | None = None


ReviewVerdict = Literal["HIT", "MISS", "UNKNOWN"]
DecisionVerdict = Literal["GOOD", "BAD", "NEUTRAL"]


class SignalReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: ReviewVerdict
    note: str = Field(default="", max_length=300)


class DecisionReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: DecisionVerdict
    note: str = Field(default="", max_length=500)


class Reflection(BaseModel):
    """reflector 출력 (스펙 §6.5 복기 2종: 신호→가격, 결정→손익)."""

    model_config = ConfigDict(extra="forbid")

    entry_id: int
    signal_review: dict[str, SignalReview] = Field(default_factory=dict)
    decision_review: DecisionReview
    lesson: str | None = Field(default=None, max_length=300)
    confidence: float = Field(ge=0, le=1)


class RoleCalibration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hit_rate: float | None
    n: int
    neighbor_hit_rate: float | None
    neighbor_n: int
    bias: float


class CalibrationReport(BaseModel):
    """35_calibration: 역할별 신호 적중률 (스펙 §9.3, 결정 60건 이상일 때만)."""

    model_config = ConfigDict(extra="forbid")

    asof: str
    n_decisions: int
    active: bool
    min_decisions: int
    roles: dict[str, RoleCalibration]


class Pick(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str
    reason: str = Field(max_length=200)


class Screened(BaseModel):
    """screener 출력 (스펙 §5.1): 오늘 분석할 종목 1~5개."""

    model_config = ConfigDict(extra="forbid")

    picks: list[Pick] = Field(min_length=1, max_length=5)
    rejected: list[str] = Field(default_factory=list)


class DiscoveredCandidate(BaseModel):
    """scout 가 발굴한 종목 하나 (Phase 19). 코드 확정·거래소·제외는 파이썬이 한다
    (`scan/discovery.resolve`)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=80)
    ticker: str | None = None
    market: Literal["KR", "US"]
    tier: Literal["universe", "listed"]
    catalyst: str = Field(max_length=200)
    source_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


class Discovered(BaseModel):
    """scout 출력: 오늘 분석할 가치가 있는 종목 ≤ 8 + 메모(악재·발굴 없음 이유)."""

    model_config = ConfigDict(extra="forbid")

    candidates: list[DiscoveredCandidate] = Field(default_factory=list, max_length=8)
    notes: list[str] = Field(default_factory=list, max_length=10)


MarketStance = Literal["RISK_ON", "NEUTRAL", "RISK_OFF"]


class HedgeProposal(BaseModel):
    """strategist 출력 (Phase 18 시장 세션): 시장 방향 판단과 헤지 비중 **제안**.

    파이썬 규칙이 최종 결정한다 — 국면 지표가 bear 가 아니면 진입하지 않고, bear 면 비중을
    [hedge.min_weight_pct, hedge.max_weight_pct] 로 자른다.
    레버리지는 거부(leveraged=false)만 가능."""

    model_config = ConfigDict(extra="forbid")

    market: Literal["KR", "US"]
    stance: MarketStance
    hedge_weight: float = Field(ge=0, le=0.25)
    leveraged: bool = False
    scalp_ok: bool = True  # Phase 23: false(확신 ≥ 기준)면 그날 분 단위 단타 금지
    confidence: float = Field(ge=0, le=1)
    horizon_days: int = Field(ge=1)
    rationale: str = Field(max_length=500)
    invalidation_conditions: list[str]


class ScalpVerdict(BaseModel):
    """분 단위 판정자(scalp-judge, Phase 25) 진입 판정. 청산 파라미터는 파이썬이 설정값으로
    클램프한다(손절을 넓히거나 보유를 늘릴 수 없다)."""

    model_config = ConfigDict(extra="forbid")

    go: bool
    size: Literal["full", "half"] = "full"
    take_profit_pct: float = Field(gt=0, le=0.05)
    stop_pct: float = Field(gt=0, le=0.03)
    max_hold_min: int = Field(ge=1, le=120)
    reason: str = Field(max_length=300)
    confidence: float = Field(ge=0, le=1)


class ScalpAdvice(BaseModel):
    """보유 중 단타의 5분 조언(Phase 25). exit 이면 다음 틱 조기 청산 — 늦추는 조언은 없다."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["hold", "exit"]
    reason: str = Field(max_length=300)
    confidence: float = Field(ge=0, le=1)


class CallMeta(BaseModel):
    """LLM 호출 기록 (체크포인트 `call` 필드)."""

    model_config = ConfigDict(extra="forbid")

    agent: str
    model: str | None = None
    session_id: str | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None


_STAGE_PATTERNS: list[tuple[re.Pattern[str], type[BaseModel]]] = [
    (re.compile(r"^10_analyst_[a-z_]+$"), AnalystReport),
    (re.compile(r"^20_debate_r\d+_(bull|bear)$"), DebateTurn),
    (re.compile(r"^30_decision$"), Decision),
    (re.compile(r"^05_quant$"), QuantReport),
    (re.compile(r"^35_calibration$"), CalibrationReport),
]


def schema_for(stage: str) -> type[BaseModel] | None:
    """체크포인트 이름에 해당하는 스키마. 파이썬이 만드는 단계(요약 등)는 None."""
    for pattern, model in _STAGE_PATTERNS:
        if pattern.match(stage):
            return model
    return None


def json_schema(model: type[BaseModel]) -> str:
    """압축 JSON Schema 문자열 (프롬프트·--json-schema 용)."""
    return json.dumps(model.model_json_schema(), separators=(",", ":"), ensure_ascii=False)
