"""KIS 브로커 도메인 모델. 금액은 Decimal."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from trader.config import Env, Market

__all__ = [
    "Candle",
    "Cash",
    "ContractSide",
    "Env",
    "FoBalance",
    "FoCash",
    "FoFill",
    "FoKind",
    "FoOrderRequest",
    "FoOrderState",
    "FoOrderable",
    "FoPosition",
    "FoQuote",
    "InvestorFlow",
    "Market",
    "OrderBook",
    "OrderRequest",
    "OrderResult",
    "Position",
    "Quote",
    "UnverifiedHolding",
]


class Quote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str
    market: Market
    price: Decimal
    change_pct: float
    volume: int
    asof: datetime  # 조회 시각 (UTC)
    # 국내만: 종목상태(iscd_stat_cls_code)·시장경고(mrkt_warn_cls_code)·임시정지 — 매매 불가 판정
    # (`risk/tradability`, Stock D-030). US·모르면 None
    status_code: str | None = None
    warn_code: str | None = None
    halted: bool | None = None


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


class RankRow(BaseModel):
    """KIS 순위 API 한 줄 (Phase 25). 필드가 없는 API 는 None. change_pct 는 소수(0.125=12.5%)."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    name: str | None = None
    rank: int
    change_pct: float | None = None
    volume: int | None = None
    net_buy: int | None = None


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


# --- 국내선물옵션(Phase 26). 기존 모델은 불변. 수량은 계약 수, 가격은 지수 포인트 ---------------

FoKind = Literal["FUTURES", "CALL", "PUT"]
ContractSide = Literal["LONG", "SHORT"]  # 계약 보유 방향(매수·매도). 시장 방향 뷰와 혼용 금지


class FoPosition(BaseModel):
    """잔고 한 행 — 방향·종류 검증을 통과한 행만. 통과하지 못한 행은 `UnverifiedHolding`."""

    model_config = ConfigDict(extra="forbid")

    code: str
    name: str | None
    kind: FoKind
    contract_side: ContractSide
    qty: int = Field(gt=0)
    avg_price: Decimal
    current_price: Decimal
    pnl: Decimal
    sellable_qty: int
    strike: Decimal | None = None
    expiry: date | None = None


class UnverifiedHolding(BaseModel):
    """방향·종류·승수 중 하나라도 확정 불가한 잔고 행. 어떤 자동 주문의 대상도 아니다(청산 포함)."""

    model_config = ConfigDict(extra="forbid")

    code: str
    raw: dict
    reason: str


class FoCash(BaseModel):
    """잔고 output2 집계. `nav` 는 `prsm_dpast_amt`(비면 None — 항등식 폴백 금지).
    `requested_at` 은 요청 시작 시각, `received_at` 은 수신 시각(예약 해제 판정은 requested_at)."""

    model_config = ConfigDict(extra="forbid")

    deposit: Decimal
    orderable_cash: Decimal
    orderable_total: Decimal
    margin_total: Decimal
    add_margin: Decimal
    nav: Decimal | None
    realized_today: Decimal
    unrealized: Decimal
    requested_at: datetime
    received_at: datetime


class FoBalance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    positions: list[FoPosition]
    unverified: list[UnverifiedHolding]
    cash: FoCash


class FoOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    side: Literal["BUY", "SELL"]
    qty: int = Field(gt=0)
    order_type: Literal["LIMIT", "MARKET"]
    price: Decimal | None = None


class FoQuote(BaseModel):
    """시세. `quote_asof` 는 응답의 시장 시각(없으면 None), `received_at` 은 수신 시각 —
    신선도는 quote_asof 로 본다. 옵션은 그릭·IV·행사가가 채워진다."""

    model_config = ConfigDict(extra="forbid")

    code: str
    kind: FoKind
    price: Decimal
    bid: Decimal | None
    ask: Decimal | None
    change_pct: float
    volume: int
    open_interest: int | None
    basis: Decimal | None
    upper: Decimal | None
    lower: Decimal | None
    last_trade_date: date | None
    days_left: int | None
    hist_vol: float | None
    iv: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    strike: Decimal | None
    underlying: Decimal | None
    underlying_asof: datetime | None
    quote_asof: datetime | None
    received_at: datetime


class FoFill(BaseModel):
    """체결 조회 한 행(원주문 계보 누적값). KIS `qty` 는 잔량, `tot_ccld_qty` 는 누적 체결."""

    model_config = ConfigDict(extra="forbid")

    order_no: str
    orgn_order_no: str | None
    code: str
    side: Literal["BUY", "SELL"]
    remaining_qty: int
    filled_qty: int
    rejected_qty: int
    cancelled_qty: int
    filled_amount: Decimal
    avg_price: Decimal | None
    filled_at: datetime


class FoOrderState(BaseModel):
    """원주문 계보(odno + orgn_odno) 의 확정 상태. terminal 은 `remaining == 0 ∨ state ∈
    {cancelled, rejected}` ∧ `filled + rejected + cancelled == order_qty`."""

    model_config = ConfigDict(extra="forbid")

    order_no: str
    order_qty: int
    filled_qty: int
    remaining_qty: int
    rejected_qty: int
    cancelled_qty: int
    terminal: bool
    state: Literal["open", "filled", "cancelled", "rejected"]


class FoOrderable(BaseModel):
    """주문가능 응답. `cash` 는 응답의 기준 현금(없으면 None → 증거금 추론 불가·신규 거부)."""

    model_config = ConfigDict(extra="forbid")

    tot_qty: int
    lqd_qty: int
    ord_qty: int
    cash: Decimal | None
    bass_idx: Decimal | None
