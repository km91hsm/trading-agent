"""파생 레인 공용 상수·행 타입(Phase 26 단계 2). 원장 `fo_positions`·`fo_order_legs` 행의 파이썬
표현이며 상태 전이 규칙은 `fo/positions.py`, 저장은 `fo/store.py` 가 맡는다."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, fields
from decimal import Decimal
from typing import Any

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
