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


def drop_incomplete_tail(candles: list[Candle]) -> list[Candle]:
    """마지막 봉이 거래량 0 이면 버린다. KIS 일봉은 개장 전·장중 초반에 오늘 봉을 거래량 0 스텁으로
    돌려주는데, 그 행이 피처 스토어에 들어가면 거래대금 0 으로 scan 이 전 종목을 거르고(실기 10-20),
    국면 proxy·종목 입력의 지표도 틀어진다. 피처·국면·수집 모두 이 함수 하나를 쓴다."""
    out = list(candles)
    while out and int(out[-1].volume) <= 0:
        out.pop()
    return out


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
    exchange: str | None = None  # US 시세용 거래소(NAS|NYS|AMS), 잔고 ovrs_excg_cd 에서. KR 은 None
    name: str | None = None  # 잔고 응답 종목명(KR prdt_name, US ovrs_item_name) → ticker_names
    # 매도 가능 수량(ord_psbl_qty). 미체결 매도·대차 등으로 보유량보다 적을 수 있다 — 손절·SELL 은
    # 이 값으로 낸다(보유량으로 내면 KIS 가 거부). None 이면 qty 로 본다
    sellable_qty: int | None = None

    @property
    def sell_qty(self) -> int:
        return self.qty if self.sellable_qty is None else min(self.qty, self.sellable_qty)


class Cash(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: Literal["KRW", "USD"]
    available: (
        Decimal  # 주문 가능 금액 (KR prvs_rcdl_excc_amt = D+2 정산 반영, 당일 체결 매수 차감됨)
    )
    total: Decimal  # 예수금 총액 (KR dnca_tot_amt — 미결제 현금이라 체결 뒤 2영업일간 과대)
    # 총평가(예수금 + 주식 평가, KR tot_evlu_amt / US 총자산). 있으면 NAV 는 이 값을 쓴다.
    # None 이면 total + 평가금액 합으로 계산(이전 방식, 체결 뒤 2영업일간 이중 계산)
    nav: Decimal | None = None


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


class Fill(BaseModel):
    """체결 1건 (체결 조회 응답). filled_at 은 UTC."""

    model_config = ConfigDict(extra="forbid")

    order_no: str
    ticker: str
    market: Market
    side: Literal["BUY", "SELL"]
    qty: int
    price: Decimal
    filled_at: datetime


class OrderResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_no: str | None
    accepted: bool
    message: str
    org_no: str | None = None  # KR 취소 시 필요한 KRX_FWDG_ORD_ORGNO
