"""계좌별 브로커: 잔고·예수금 조회 (체결 조회는 Phase 6)."""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from trader.broker.errors import KisApiError
from trader.broker.kis_client import KisClient
from trader.broker.models import Cash, Market, OrderRequest, OrderResult, Position
from trader.broker.tr_ids import path
from trader.config import AccountConfig

logger = logging.getLogger("trader.kis")

# 미확인: NASD 고정 조회에 NYS/AMS 보유분이 포함되는지는 실기에서 확인한다 (TODO).
US_EXCHANGE_CODE = "NASD"


def _dec(v: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(v).strip() or default)
    except InvalidOperation:
        return Decimal(default)


def _int(v: Any) -> int:
    return int(_dec(v))


def _rows(v: Any) -> list[dict]:
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        return [v]
    return []


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

    def get_positions(self, market: Market) -> list[Position]:
        """보유 종목 (수량 0 제외)."""
        self._check_market(market)
        out: list[Position] = []
        if market == "KR":
            for r in _rows(self._balance_kr().get("output1")):
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
                )
            )
        return out

    def get_cash(self, market: Market) -> Cash:
        """예수금. KR 은 잔고 응답의 예수금·D+2 정산금, US 는 주문가능외화금액."""
        self._check_market(market)
        if market == "KR":
            rows = _rows(self._balance_kr().get("output2"))
            summary = rows[0] if rows else {}
            return Cash(
                currency="KRW",
                available=_dec(summary.get("prvs_rcdl_excc_amt", summary.get("dnca_tot_amt"))),
                total=_dec(summary.get("dnca_tot_amt")),
            )
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
                "ORD_UNPR": _price_str(request.price) if request.order_type == "LIMIT" else "0",
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
                "OVRS_ORD_UNPR": _price_str(request.price),
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


# 주문용 거래소 코드 (시세 EXCD 와 다르다): NAS→NASD, NYS→NYSE, AMS→AMEX
US_ORDER_EXCHANGE = {"NAS": "NASD", "NYS": "NYSE", "AMS": "AMEX"}


def _us_order_exchange(exchange: str | None) -> str:
    if not exchange:
        raise ValueError("US 주문에는 exchange(NAS|NYS|AMS)가 필요합니다")
    if exchange in US_ORDER_EXCHANGE.values():
        return exchange
    try:
        return US_ORDER_EXCHANGE[exchange]
    except KeyError:
        raise ValueError(f"알 수 없는 US 거래소: {exchange}") from None


def _price_str(price: Decimal | None) -> str:
    if price is None:
        return "0"
    s = format(price.normalize(), "f")
    return s
