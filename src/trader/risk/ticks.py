"""호가단위 (스펙 §6 주문): LLM 지정가를 거래소가 받는 가격으로 맞춘다.

KR 은 KRX 호가단위(2023-01 개정) 표, US 는 $1 이상 0.01·미만 0.0001. 가장 가까운 단위로 맞춘다
(LLM 의 의도 가격을 보존; 내림·올림으로 방향을 바꾸지 않는다). 거래소가 거부하지 않는 유일한 장치라
`rules.evaluate` 가 판정 가격을 만들 때 적용하고 `accounts._price_str` 이 보내기 전에 검증한다.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# (상한 미만, 호가단위) — 오름차순
KR_TICKS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("2000"), Decimal("1")),
    (Decimal("5000"), Decimal("5")),
    (Decimal("20000"), Decimal("10")),
    (Decimal("50000"), Decimal("50")),
    (Decimal("200000"), Decimal("100")),
    (Decimal("500000"), Decimal("500")),
)
KR_TICK_TOP = Decimal("1000")
US_TICK = Decimal("0.01")
US_TICK_SUB_DOLLAR = Decimal("0.0001")


def tick_size(market: str, price: Decimal) -> Decimal:
    if market == "US":
        return US_TICK if price >= 1 else US_TICK_SUB_DOLLAR
    for bound, tick in KR_TICKS:
        if price < bound:
            return tick
    return KR_TICK_TOP


def quantize_price(market: str, price: Decimal) -> Decimal:
    """가장 가까운 호가단위로. 반올림 결과가 상위 구간으로 넘어가면 그 구간 단위로 다시 맞춘다."""
    tick = tick_size(market, price)
    q = (price / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick
    if tick_size(market, q) != tick:  # 구간 경계(예 1999.6 → 2000)
        tick = tick_size(market, q)
        q = (q / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick
    return q.quantize(tick) if tick < 1 else q.quantize(Decimal("1"))


def is_on_tick(market: str, price: Decimal) -> bool:
    return quantize_price(market, price) == price


# --- 국내 파생(Phase 26): 상품 설정의 호가단위 표 `[(상한 미만|None, tick), ...]` ----------------


def tick_table_for(product, kind: str) -> list[tuple[Decimal | None, Decimal]]:
    """선물은 단일 단위, 옵션은 가격 구간표(`FoProductCfg.option_ticks`, 마지막 상한 None)."""
    if kind == "FUTURES":
        return [(None, Decimal(product.futures_tick))]
    return [(None if b is None else Decimal(b), Decimal(t)) for b, t in product.option_ticks]


def tick_for(table: list[tuple[Decimal | None, Decimal]], price: Decimal) -> Decimal:
    for bound, tick in table:
        if bound is None or price < bound:
            return tick
    return table[-1][1]


def quantize_tick(table: list[tuple[Decimal | None, Decimal]], price: Decimal) -> Decimal:
    """가장 가까운 단위로. 반올림 결과가 다른 구간으로 넘어가면 그 구간 단위로 다시 맞춘다."""
    tick = tick_for(table, price)
    q = (price / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick
    if tick_for(table, q) != tick:
        tick = tick_for(table, q)
        q = (q / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick
    return q.quantize(tick)


def is_on_tick_table(table: list[tuple[Decimal | None, Decimal]], price: Decimal) -> bool:
    return quantize_tick(table, price) == price
