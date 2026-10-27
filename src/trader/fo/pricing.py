"""옵션 가격모형(Black-76 근사, 무위험이자 0). 시나리오 스트레스(지수 ±·IV 상승) 재평가에만 쓴다 —
주문 가격이나 신호에는 쓰지 않는다. 입력은 시세의 IV·잔존일."""

from __future__ import annotations

import math
from decimal import Decimal

from trader.broker.models import FoKind, FoQuote

YEAR_DAYS = 365.0
_Q = Decimal("0.0001")


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def price(kind: FoKind, F: Decimal, K: Decimal, iv: float, days_left: int) -> Decimal:
    """선물은 F 그대로. 옵션은 Black-76(r=0); 잔존일 ≤ 0 또는 iv ≤ 0 이면 내재가치."""
    if kind == "FUTURES":
        return F
    f, k = float(F), float(K)
    intrinsic = max(f - k, 0.0) if kind == "CALL" else max(k - f, 0.0)
    if days_left <= 0 or iv <= 0 or f <= 0 or k <= 0:
        return Decimal(str(intrinsic)).quantize(_Q)
    t = days_left / YEAR_DAYS
    s = iv * math.sqrt(t)
    d1 = (math.log(f / k) + 0.5 * iv * iv * t) / s
    d2 = d1 - s
    if kind == "CALL":
        v = f * _norm_cdf(d1) - k * _norm_cdf(d2)
    else:
        v = k * _norm_cdf(-d2) - f * _norm_cdf(-d1)
    return Decimal(str(max(v, intrinsic))).quantize(_Q)


def reprice(
    quote: FoQuote, *, index_shift_pct: float, iv_rel: float, days_left: int
) -> Decimal:
    """시나리오 가격: 기초지수 × (1 + shift), IV × (1 + iv_rel). 선물은 선형(현재가 기준),
    옵션은 행사가·IV·기초지수가 있어야 한다(없으면 ValueError — 호출자가 신규를 거부)."""
    if quote.kind == "FUTURES":
        return (quote.price * (Decimal(1) + Decimal(str(index_shift_pct)))).quantize(_Q)
    if quote.strike is None or quote.iv is None or quote.underlying is None:
        raise ValueError(f"옵션 재평가 입력 부족: {quote.code} (행사가·IV·기초지수 필요)")
    F = quote.underlying * (Decimal(1) + Decimal(str(index_shift_pct)))
    iv = quote.iv * (1.0 + iv_rel)
    return price(quote.kind, F, quote.strike, iv, days_left)
