"""KIS 브로커 도메인 모델. 금액은 Decimal."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from trader.config import Env, Market

__all__ = [
    "Candle",
    "Cash",
    "Env",
    "InvestorFlow",
    "Market",
    "OrderBook",
    "OrderRequest",
    "OrderResult",
    "Position",
    "Quote",
]


class Quote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str
    market: Market
    price: Decimal
    change_pct: float
    volume: int
    asof: datetime  # 조회 시각 (UTC)


class Candle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    time: str | None = None  # 분봉 체결 시각 "HHMMSS" (일봉은 None)


class InvestorFlow(BaseModel):
    """투자자별 일별 순매수 수량."""

    model_config = ConfigDict(extra="forbid")

    date: date
    foreign_net: int
    institution_net: int
    individual_net: int


class OrderBook(BaseModel):
    """호가. asks/bids 는 1호가부터 (가격, 잔량) 순, 빈 호가는 제외."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    market: Market
    asks: list[tuple[Decimal, int]]
    bids: list[tuple[Decimal, int]]
    asof: datetime


class Position(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str
    market: Market
    qty: int
    avg_price: Decimal
    current_price: Decimal
    eval_amount: Decimal
    pnl_pct: float


class Cash(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: Literal["KRW", "USD"]
    available: Decimal  # 주문 가능 금액
    total: Decimal  # 예수금 총액


class OrderRequest(BaseModel):
    """exchange 는 기본값이 없다. KR 주문도 exchange=None 을 명시한다."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    market: Market
    side: Literal["BUY", "SELL"]
    qty: int
    order_type: Literal["LIMIT", "MARKET"]
    price: Decimal | None
    exchange: str | None


class OrderResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_no: str | None
    accepted: bool
    message: str
    org_no: str | None = None  # KR 취소 시 필요한 KRX_FWDG_ORD_ORGNO
