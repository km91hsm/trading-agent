"""KIS 응답 필드 파싱 (시세·잔고 모듈 공용). 숫자는 문자열로 오고 결측은 ''/None 이다."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def dec(v: Any, default: str = "0") -> Decimal:
    """''·None·해석 불가 → default. 금액은 Decimal 로만 다룬다."""
    try:
        return Decimal(str(v).strip() or default)
    except InvalidOperation:
        return Decimal(default)


def to_int(v: Any) -> int:
    return int(dec(v))


def rows(v: Any) -> list[dict]:
    """output1/output2 는 리스트·dict·빈 값이 섞여 온다. 비어 있지 않은 dict 만 리스트로."""
    if isinstance(v, list):
        return [r for r in v if isinstance(r, dict) and r]
    if isinstance(v, dict) and v:
        return [v]
    return []
