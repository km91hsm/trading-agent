"""파생 레인 공용 상수·행 타입(Phase 26 단계 2). 원장 `fo_positions`·`fo_order_legs` 행의 파이썬
표현이며 상태 전이 규칙은 `fo/positions.py`, 저장은 `fo/store.py` 가 맡는다."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, fields
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from trader.broker.fo_master import Contract
    from trader.broker.models import FoCash, FoOrderable, FoQuote
    from trader.config import FoCfg
    from trader.fo.schemas import FoDecision, FoDecisionEnvelope

POSITION_STATUSES = (
    "entering",
    "held",
    "resizing",
    "closing",
    "exit_failed",
    "awaiting_settlement",
    "settled",
    "closed",
    "unknown",
)
# 자리(종목 수·계약 수)를 차지하는 상태 — settled·closed 만 빠진다
OPEN_POSITION_STATUSES = (
    "entering",
    "held",
    "resizing",
    "closing",
    "exit_failed",
    "awaiting_settlement",
    "unknown",
)
LEG_STATUSES = (
    "prepared",
    "sending",
    "accepted",
    "unknown",
    "rejected",
    "cancelled",
    "filled",
    "expired",
)
LIVE_LEG = ("prepared", "sending", "accepted", "unknown")
ENTRY_INTENTS = ("OPEN", "RESIZE_UP")
REDUCE_INTENTS = ("RESIZE_DOWN", "CLOSE", "STOP", "EXPIRY", "SESSION_CLOSE", "RISK_REDUCE")

# 포지션 상태 전이표(스펙 §5). 저장 계층 `store.transition`·`store.reserve` 가 이 표 밖의 전이를
# 거부한다(단계 2 리뷰 C2-10) — 여기 하나가 정본이고 `fo/positions` 는 다시 내보낸다.
TRANSITIONS: dict[str, set[str]] = {
    "entering": {"held", "closed", "unknown", "resizing", "closing"},
    "held": {"resizing", "closing", "unknown", "awaiting_settlement"},
    "resizing": {"held", "closing", "closed", "unknown", "exit_failed"},
    "closing": {"closed", "exit_failed", "unknown", "awaiting_settlement", "held"},
    "exit_failed": {"closing", "closed", "unknown", "held"},  # held 는 감액 목표 도달(G2-7)
    "awaiting_settlement": {"settled", "unknown"},
    "unknown": {"held", "closed"},  # 사람(adopt·close)만
    "settled": set(),
    "closed": set(),
}


def can_transition(a: str, b: str) -> bool:
    return b in TRANSITIONS.get(a, set())

_DEC_FIELDS = frozenset(
    {
        "stop_pt",
        "stop_fut",
        "tp_pt",
        "premium_stop",
        "entry_premium",
        "nav_at_open",
        "realized_loss",
        "fees",
        "price",
    }
)


def is_reduce(intent: str) -> bool:
    return intent in REDUCE_INTENTS


def is_entry(intent: str) -> bool:
    return intent in ENTRY_INTENTS


def _dec(v: Any) -> Decimal | None:
    return None if v is None or v == "" else Decimal(str(v))


@dataclass(frozen=True)
class FoPositionRow:
    id: int
    account_id: str
    code: str
    kind: str
    contract_side: str
    direction: str
    vehicle: str
    status: str
    qty_target: int
    qty_held: int
    leverage_used: float | None
    stop_pt: Decimal | None
    stop_fut: Decimal | None
    tp_pt: Decimal | None
    approved_mult: float | None
    premium_stop: Decimal | None
    entry_premium: Decimal | None
    nav_at_open: Decimal | None
    realized_loss: Decimal
    fees: Decimal
    active_reduce_leg_id: int | None
    horizon_until: str | None
    last_trade_date: str | None
    decision_date: str | None
    round: int
    version: int
    opened_at: str | None
    closed_at: str | None
    note: str | None
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> FoPositionRow:
        return cls(**_coerce(cls, row))


@dataclass(frozen=True)
class FoLegRow:
    id: int
    position_id: int
    account_id: str
    intent: str
    round: int
    attempt: int
    status: str
    order_id: int | None
    order_no: str | None
    qty: int
    filled_qty: int
    price: Decimal | None
    order_type: str | None
    sent_at: str | None
    accepted_at: str | None
    created_at: str
    request_id: str | None = None

    @property
    def remaining(self) -> int:
        return max(self.qty - self.filled_qty, 0)

    @property
    def live(self) -> bool:
        return self.status in LIVE_LEG

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> FoLegRow:
        return cls(**_coerce(cls, row))


def _coerce(cls, row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    out: dict[str, Any] = {}
    for f in fields(cls):
        v = row[f.name] if f.name in keys else None
        if f.name in _DEC_FIELDS:
            v = _dec(v)
            if v is None and f.name in ("realized_loss", "fees"):
                v = Decimal(0)
        out[f.name] = v
    return out


# --- 게이트 입력·출력(스펙 §4) ----------------------------------------------------------


@dataclass
class FoPortfolio:
    """계좌 상태 묶음: 브로커 잔고(NAV·현금) + 원장(열린 포지션·살아 있는 다리) + 차단 사유 +
    수단별 검증 플래그."""

    account_id: str
    nav: Decimal | None
    cash: FoCash | None
    positions: list[FoPositionRow] = field(default_factory=list)
    legs: list[FoLegRow] = field(default_factory=list)
    blocking: str | None = None
    halted: bool = False
    verified: dict[str, bool] = field(default_factory=dict)
    realized_today: Decimal = Decimal(0)
    # 오늘 손익(실현 + 평가 변화분, `executor.pnl_today`). None 이면 실현만(기준 관측 전)
    pnl_today: Decimal | None = None


@dataclass
class GateInput:
    decision: FoDecision
    env: FoDecisionEnvelope
    port: FoPortfolio
    cfg: FoCfg
    quote: FoQuote | None
    underlying: Decimal | None
    index_asof: datetime | None
    regime: str
    view_state: str
    now: datetime
    today: date
    killswitch: bool = False
    paused: bool = False
    orderable: FoOrderable | None = None
    inputs_fresh: bool = True
    historical: bool = False
    contract: Contract | None = None
    days_left: int | None = None
    position: FoPositionRow | None = None
    # 이 날부터 신규 금지(만기 강제 청산일의 직전 운영일, 휴장 반영 — 엔진의 강제 청산과 같은 달력)
    entry_block_day: date | None = None
    # 새 포지션이 이어받는 사용 예산(REPLACE 교체 — 옛 포지션의 실현 손실 + 비용)
    carry_used: Decimal = Decimal(0)


@dataclass
class Leg:
    """판정이 만든 주문 다리 하나(진입 또는 축소)."""

    intent: str
    order_side: str
    qty: int
    order_type: str
    price: Decimal | None
    planned_loss: Decimal
    code: str
    kind: str
    position_id: int | None = None


@dataclass
class SizeResult:
    n_target: int
    n_leg: int
    planned_loss_leg: Decimal
    planned_loss_per_contract: Decimal
    exposure_after: Decimal
    stress_loss_after: Decimal
    budget: dict[str, Any]
    ref_price: Decimal | None
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # 보유(평균가 기준) + 살아 있는 진입 다리(다리 가격 기준)의 계획 손실 — 예약 재검사가 받는다
    planned_loss_existing: Decimal = Decimal(0)


@dataclass
class FoVerdict:
    approved: bool
    legs: list[Leg] = field(default_factory=list)
    leverage_used: float = 0.0
    exposure_after: Decimal = Decimal(0)
    stress_loss_after: Decimal = Decimal(0)
    lifetime_budget: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    note: str = ""
    cross_factor: float = 1.0
    stop_pt: Decimal | None = None
    approved_mult: float | None = None
    premium_stop: Decimal | None = None
    n_target: int = 0
    contract_side: str | None = None
    kind: str | None = None
    code: str | None = None
    planned_loss_existing: Decimal = Decimal(0)
    # 목표 < 보유 + 미체결 진입: 다리 없이 미체결 진입을 회수한 뒤 다시 계산하라는 요청(G2-5)
    recall_entries: bool = False

    def as_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        d = asdict(self)
        return _plain(d)


def _plain(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    return v
