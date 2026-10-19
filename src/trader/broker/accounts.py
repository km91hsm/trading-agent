"""계좌별 브로커: 잔고·예수금 조회 (체결 조회는 Phase 6)."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from trader.broker._parse import dec as _dec
from trader.broker._parse import rows as _rows
from trader.broker._parse import to_int as _int
from trader.broker.errors import KisApiError
from trader.broker.kis_client import KisClient
from trader.broker.models import Cash, Fill, Market, OrderRequest, OrderResult, Position
from trader.broker.tr_ids import path
from trader.config import KST, AccountConfig

logger = logging.getLogger("trader.kis")

# 미확인: NASD 고정 조회에 NYS/AMS 보유분이 포함되는지는 실기에서 확인한다 (TODO).
US_EXCHANGE_CODE = "NASD"


class KisBroker:
    def __init__(self, account: AccountConfig, client: KisClient) -> None:
        if client.env != account.env:
            raise ValueError(
                f"계좌 '{account.id}' env({account.env})와 클라이언트 env({client.env}) 불일치"
            )
        self.account = account
        self._client = client

    def _check_market(self, market: Market) -> None:
        if market not in self.account.markets:
            raise ValueError(f"계좌 '{self.account.id}' 는 {market} 시장을 쓰지 않습니다")

    def _acct(self) -> dict[str, str]:
        return {"CANO": self.account.cano, "ACNT_PRDT_CD": self.account.acnt_prdt_cd}

    def _balance_kr(self) -> dict:
        params = {
            **self._acct(),
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        return self._client.get(path("DOM_BALANCE"), "DOM_BALANCE", params)

    def _balance_us(self) -> dict:
        params = {
            **self._acct(),
            "OVRS_EXCG_CD": US_EXCHANGE_CODE,
            "TR_CRCY_CD": "USD",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }
        return self._client.get(path("OVS_BALANCE"), "OVS_BALANCE", params)

    def get_balance(self, market: Market) -> tuple[list[Position], Cash]:
        """(보유 종목, 예수금) 을 한 번에. KR 은 inquire-balance 응답 하나(output1·output2)에서 둘을
        뽑아 같은 API 를 두 번 부르지 않는다(모의 2/s). US 는 잔고·예수금 API 가 달라 두 번."""
        self._check_market(market)
        if market == "KR":
            data = self._balance_kr()
            return self._positions_kr(data), self._cash_kr(data)
        return self.get_positions(market), self.get_cash(market)

    def _positions_kr(self, data: dict) -> list[Position]:
        out: list[Position] = []
        for r in _rows(data.get("output1")):
            qty = _int(r.get("hldg_qty"))
            if qty <= 0:
                continue
            out.append(
                Position(
                    ticker=str(r["pdno"]),
                    market="KR",
                    qty=qty,
                    avg_price=_dec(r.get("pchs_avg_pric")),
                    current_price=_dec(r.get("prpr")),
                    eval_amount=_dec(r.get("evlu_amt")),
                    pnl_pct=float(_dec(r.get("evlu_pfls_rt"))) / 100,
                )
            )
        return out

    def _cash_kr(self, data: dict) -> Cash:
        rows = _rows(data.get("output2"))
        summary = rows[0] if rows else {}
        return Cash(
            currency="KRW",
            available=_dec(summary.get("prvs_rcdl_excc_amt", summary.get("dnca_tot_amt"))),
            total=_dec(summary.get("dnca_tot_amt")),
        )

    def get_positions(self, market: Market) -> list[Position]:
        """보유 종목 (수량 0 제외)."""
        self._check_market(market)
        out: list[Position] = []
        if market == "KR":
            return self._positions_kr(self._balance_kr())
        for r in _rows(self._balance_us().get("output1")):
            qty = _int(r.get("ovrs_cblc_qty"))
            if qty <= 0:
                continue
            out.append(
                Position(
                    ticker=str(r["ovrs_pdno"]),
                    market="US",
                    qty=qty,
                    avg_price=_dec(r.get("pchs_avg_pric")),
                    current_price=_dec(r.get("now_pric2")),
                    eval_amount=_dec(r.get("ovrs_stck_evlu_amt")),
                    pnl_pct=float(_dec(r.get("evlu_pfls_rt"))) / 100,
                    exchange=US_QUOTE_EXCHANGE.get(str(r.get("ovrs_excg_cd") or "").upper()),
                )
            )
        return out

    def get_cash(self, market: Market) -> Cash:
        """예수금. KR 은 잔고 응답의 예수금·D+2 정산금, US 는 주문가능외화금액."""
        self._check_market(market)
        if market == "KR":
            return self._cash_kr(self._balance_kr())
        # 주문가능금액 API(inquire-psamount)는 종목코드가 필수라 현금 조회엔 못 쓴다 (실기 확인).
        params = {
            **self._acct(),
            "WCRC_FRCR_DVSN_CD": "02",  # 외화
            "NATN_CD": "840",  # 미국
            "TR_MKET_CD": "00",
            "INQR_DVSN_CD": "00",
        }
        data = self._client.get(path("OVS_PRESENT_BALANCE"), "OVS_PRESENT_BALANCE", params)
        usd = next(
            (r for r in _rows(data.get("output2")) if str(r.get("crcy_cd", "")).upper() == "USD"),
            {},
        )
        return Cash(
            currency="USD",
            available=_dec(usd.get("frcr_drwg_psbl_amt_1")),
            total=_dec(usd.get("frcr_dncl_amt_2")),
        )

    # --- 체결 조회 (Phase 6 fills 동기화) ---------------------------------------------

    def get_fills(self, market: Market, d: date) -> list[Fill]:
        """해당 날짜의 체결 내역 (체결수량 0 행 제외). 시각은 KR=KST, US=ET 를 UTC 로 바꾼다."""
        self._check_market(market)
        ymd = d.strftime("%Y%m%d")
        if market == "KR":
            params = {
                **self._acct(),
                "INQR_STRT_DT": ymd,
                "INQR_END_DT": ymd,
                "SLL_BUY_DVSN_CD": "00",
                "INQR_DVSN": "00",
                "PDNO": "",
                "CCLD_DVSN": "01",
                "ORD_GNO_BRNO": "",
                "ODNO": "",
                "INQR_DVSN_3": "00",
                "INQR_DVSN_1": "",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            }
            data = self._client.get(path("DOM_CCLD"), "DOM_CCLD", params)
            out: list[Fill] = []
            for r in _rows(data.get("output1")):
                qty = _int(r.get("tot_ccld_qty"))
                if qty <= 0:
                    continue
                out.append(
                    Fill(
                        order_no=str(r.get("odno") or ""),
                        ticker=str(r.get("pdno") or ""),
                        market="KR",
                        side="SELL" if str(r.get("sll_buy_dvsn_cd")) == "01" else "BUY",
                        qty=qty,
                        price=_dec(r.get("avg_prvs")),
                        filled_at=_local_dt(r.get("ord_dt"), r.get("ord_tmd"), KST),
                    )
                )
            return out
        params = {
            **self._acct(),
            "PDNO": "",
            "ORD_STRT_DT": ymd,
            "ORD_END_DT": ymd,
            "SLL_BUY_DVSN": "00",
            "CCLD_NCCS_DVSN": "00",
            "OVRS_EXCG_CD": US_EXCHANGE_CODE,
            "SORT_SQN": "DS",
            "ORD_DT": "",
            "ORD_GNO_BRNO": "",
            "ODNO": "",
            "CTX_AREA_NK200": "",
            "CTX_AREA_FK200": "",
        }
        data = self._client.get(path("OVS_CCLD_US"), "OVS_CCLD_US", params)
        out = []
        for r in _rows(data.get("output")):
            qty = _int(r.get("ft_ccld_qty"))
            if qty <= 0:
                continue
            out.append(
                Fill(
                    order_no=str(r.get("odno") or ""),
                    ticker=str(r.get("pdno") or ""),
                    market="US",
                    side="SELL" if str(r.get("sll_buy_dvsn_cd")) == "01" else "BUY",
                    qty=qty,
                    price=_dec(r.get("ft_ccld_unpr3")),
                    filled_at=_local_dt(r.get("ord_dt"), r.get("ord_tmd"), _ET),
                )
            )
        return out

    # --- 주문 (Phase 4). 호출은 orchestrator/executor 만 한다. -------------------------

    def place_order(self, request: OrderRequest) -> OrderResult:
        """현금 주문. 접수 실패(rt_cd≠0)는 KisApiError. US 는 지정가만(모의 서버 제약)."""
        self._check_market(request.market)
        if request.market == "KR":
            name = "DOM_ORDER_BUY" if request.side == "BUY" else "DOM_ORDER_SELL"
            body = {
                **self._acct(),
                "PDNO": request.ticker,
                "ORD_DVSN": "00" if request.order_type == "LIMIT" else "01",
                "ORD_QTY": str(request.qty),
                "ORD_UNPR": (
                    _price_str(request.price, "KR") if request.order_type == "LIMIT" else "0"
                ),
            }
        else:
            if request.order_type != "LIMIT" or request.price is None:
                raise KisApiError("UNSUPPORTED", "US 주문은 지정가만 지원합니다")
            name = "OVS_ORDER_BUY_US" if request.side == "BUY" else "OVS_ORDER_SELL_US"
            body = {
                **self._acct(),
                "OVRS_EXCG_CD": _us_order_exchange(request.exchange),
                "PDNO": request.ticker,
                "ORD_QTY": str(request.qty),
                "OVRS_ORD_UNPR": _price_str(request.price, "US"),
                "ORD_SVR_DVSN_CD": "0",
                "ORD_DVSN": "00",
            }
            if request.side == "SELL":
                body["SLL_TYPE"] = "00"
        data = self._client.post(path(name), name, body)
        out = data.get("output") or {}
        return OrderResult(
            order_no=str(out.get("ODNO") or "") or None,
            accepted=True,
            message=str(data.get("msg1") or ""),
            org_no=str(out.get("KRX_FWDG_ORD_ORGNO") or "") or None,
        )

    def cancel_order(
        self,
        order_no: str,
        *,
        market: Market = "KR",
        org_no: str | None = None,
        ticker: str | None = None,
        exchange: str | None = None,
        qty: int = 0,
    ) -> OrderResult:
        """미체결 전량 취소. US 는 ticker·exchange·qty 가 필요하다."""
        self._check_market(market)
        if market == "KR":
            name = "DOM_ORDER_CANCEL"
            body = {
                **self._acct(),
                "KRX_FWDG_ORD_ORGNO": org_no or "",
                "ORGN_ODNO": order_no,
                "ORD_DVSN": "00",
                "RVSE_CNCL_DVSN_CD": "02",
                "ORD_QTY": "0",
                "ORD_UNPR": "0",
                "QTY_ALL_ORD_YN": "Y",
            }
        else:
            if not ticker or qty <= 0:
                raise ValueError("US 주문 취소에는 ticker 와 qty 가 필요합니다")
            name = "OVS_ORDER_CANCEL_US"
            body = {
                **self._acct(),
                "OVRS_EXCG_CD": _us_order_exchange(exchange),
                "PDNO": ticker,
                "ORGN_ODNO": order_no,
                "RVSE_CNCL_DVSN_CD": "02",
                "ORD_QTY": str(qty),
                "OVRS_ORD_UNPR": "0",
            }
        data = self._client.post(path(name), name, body)
        out = data.get("output") or {}
        return OrderResult(
            order_no=str(out.get("ODNO") or "") or order_no,
            accepted=True,
            message=str(data.get("msg1") or ""),
        )


_ET = ZoneInfo("America/New_York")


def _local_dt(ymd: Any, hms: Any, tz: ZoneInfo) -> datetime:
    """'20261009' + '100512' 를 해당 시간대로 해석해 UTC 로 돌려준다 (시각 없으면 00:00)."""
    s = f"{ymd or ''}{str(hms or '000000').zfill(6)}"
    try:
        local = datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=tz)
    except ValueError:
        local = datetime.strptime(str(ymd), "%Y%m%d").replace(tzinfo=tz)
    return local.astimezone(UTC)


# 주문용 거래소 코드 (시세 EXCD 와 다르다): NAS→NASD, NYS→NYSE, AMS→AMEX
US_ORDER_EXCHANGE = {"NAS": "NASD", "NYS": "NYSE", "AMS": "AMEX"}
US_QUOTE_EXCHANGE = {v: k for k, v in US_ORDER_EXCHANGE.items()}  # 잔고 ovrs_excg_cd → 시세 EXCD


def _us_order_exchange(exchange: str | None) -> str:
    if not exchange:
        raise ValueError("US 주문에는 exchange(NAS|NYS|AMS)가 필요합니다")
    if exchange in US_ORDER_EXCHANGE.values():
        return exchange
    try:
        return US_ORDER_EXCHANGE[exchange]
    except KeyError:
        raise ValueError(f"알 수 없는 US 거래소: {exchange}") from None


def _price_str(price: Decimal | None, market: str | None = None) -> str:
    """주문 단가 문자열. market 을 주면 호가단위 밖 가격을 보내기 전에 ValueError 로 막는다."""
    if price is None:
        return "0"
    if market is not None:
        from trader.risk.ticks import is_on_tick

        if not is_on_tick(market, price):
            raise ValueError(f"호가단위 밖 지정가 {price} ({market})")
    s = format(price.normalize(), "f")
    return s
