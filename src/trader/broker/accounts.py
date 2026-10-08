"""계좌별 브로커: 잔고·예수금 조회 (주문·취소·체결은 Phase 4)."""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

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
        params = {
            **self._acct(),
            "OVRS_EXCG_CD": US_EXCHANGE_CODE,
            "OVRS_ORD_UNPR": "",
            "ITEM_CD": "",
        }
        data = self._client.get(path("OVS_PSBL_ORDER"), "OVS_PSBL_ORDER", params)
        rows = _rows(data.get("output"))
        out = rows[0] if rows else {}
        available = _dec(out.get("ord_psbl_frcr_amt", out.get("frcr_ord_psbl_amt1")))
        return Cash(currency="USD", available=available, total=available)

    def place_order(self, request: OrderRequest | None) -> OrderResult:
        raise NotImplementedError("주문은 Phase 4 에서 구현합니다")

    def cancel_order(self, order_no: str) -> OrderResult:
        raise NotImplementedError("주문 취소는 Phase 4 에서 구현합니다")
