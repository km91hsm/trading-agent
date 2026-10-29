"""실제 적용 손절선 — 장중 모니터와 웹 보유 표가 같은 함수를 쓴다(화면이 결정 원문만 보여 주면
모니터가 쓰는 손절 바닥·기본 손절이 보이지 않는다).

우선순위: 보유 묶음 시작 뒤 결정들의 손절 바닥(`repo.max_stop_since`)이 결정 손절가보다 높으면
바닥(검토 L5 — 손절선은 올리기만 한다), 아니면 결정 손절가, 둘 다 없으면 평단 × (1 + 기본 비율).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

StopSource = Literal["decision", "floor", "default"]


def effective_stop(
    decision_stop, floor: Decimal | None, avg_price: Decimal | None, default_pct: float
) -> tuple[Decimal | None, StopSource | None]:
    """(손절선, 출처). 결정 손절가 0·None 은 없음으로 본다. 근거가 하나도 없으면 (None, None)."""
    cur = Decimal(str(decision_stop)) if decision_stop else None
    if floor is not None and (cur is None or floor > cur):
        return floor, "floor"
    if cur is not None:
        return cur, "decision"
    if avg_price is not None and avg_price > 0:
        return avg_price * (1 + Decimal(str(default_pct))), "default"
    return None, None
