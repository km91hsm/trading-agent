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

# US 잔고·체결은 거래소 코드별로 조회된다(NASD 고정이면 NYSE·AMEX 보유분이 보이지 않아 손절·헤지
# 해제·체결 동기화가 전부 빠진다) → 세 거래소를 모두 돌려 합친다. 모의 2/s 라 잔고 1회당 3호출.
US_EXCHANGE_CODES = ("NASD", "NYSE", "AMEX")
US_EXCHANGE_CODE = US_EXCHANGE_CODES[0]  # 호환용


def _name(v: Any) -> str | None:
    """잔고 응답의 종목명. 빈 값은 None."""
    s = str(v or "").strip()
    return s or None


class KisBroker:
    def __init__(self, account: AccountConfig, client: KisClient) -> None:
        if client.env != account.env:
            raise ValueError(
                f"계좌 '{account.id}' env({account.env})와 클라이언트 env({client.env}) 불일치"
            )
        self.account = account
        self._client = client

    @property
    def client(self):
        return self._client

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
        """세 거래소 잔고를 합친다(output1 은 종목·거래소로 중복 제거, output2 는 첫 응답)."""
        merged: dict = {"output1": [], "output2": None}
        seen: set[tuple[str, str]] = set()
        for code in US_EXCHANGE_CODES:
            params = {
                **self._acct(),
                "OVRS_EXCG_CD": code,
                "TR_CRCY_CD": "USD",
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            }
            data = self._client.get(path("OVS_BALANCE"), "OVS_BALANCE", params)
            for r in _rows(data.get("output1")):
                key = (str(r.get("ovrs_pdno") or ""), str(r.get("ovrs_excg_cd") or code))
                if key in seen:
                    continue
                seen.add(key)
                merged["output1"].append(r)
            if merged["output2"] is None and data.get("output2") is not None:
                merged["output2"] = data.get("output2")
        return merged

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
                    name=_name(r.get("prdt_name")),
                    sellable_qty=_int(r["ord_psbl_qty"]) if r.get("ord_psbl_qty") else None,
                )
            )
        return out

    def _cash_kr(self, data: dict) -> Cash:
        rows = _rows(data.get("output2"))
        summary = rows[0] if rows else {}
        nav_raw = summary.get("tot_evlu_amt") or summary.get("nass_amt")
        return Cash(
            currency="KRW",
            available=_dec(summary.get("prvs_rcdl_excc_amt", summary.get("dnca_tot_amt"))),
            total=_dec(summary.get("dnca_tot_amt")),
            nav=_dec(nav_raw) if nav_raw not in (None, "") else None,
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
                    name=_name(r.get("ovrs_item_name")),
                    sellable_qty=_int(r["ord_psbl_qty"]) if r.get("ord_psbl_qty") else None,
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
        o3 = _rows(data.get("output3"))
        tot = (o3[0] if o3 else {}).get("tot_asst_amt") if o3 else None
        return Cash(
            currency="USD",
            available=_dec(usd.get("frcr_drwg_psbl_amt_1")),
            total=_dec(usd.get("frcr_dncl_amt_2")),
            # 총자산(output3.tot_asst_amt, 외화 평가 합) — 실기 확인 항목. 없으면 None → 합산 방식
            nav=_dec(tot) if tot not in (None, "") else None,
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
        out = []
        seen_us: set[tuple[str, str, str]] = set()
        rows_us: list[dict] = []
        for code in US_EXCHANGE_CODES:
            params = {
                **self._acct(),
                "PDNO": "",
                "ORD_STRT_DT": ymd,
                "ORD_END_DT": ymd,
                "SLL_BUY_DVSN": "00",
                "CCLD_NCCS_DVSN": "00",
                "OVRS_EXCG_CD": code,
                "SORT_SQN": "DS",
                "ORD_DT": "",
                "ORD_GNO_BRNO": "",
                "ODNO": "",
                "CTX_AREA_NK200": "",
                "CTX_AREA_FK200": "",
            }
            data = self._client.get(path("OVS_CCLD_US"), "OVS_CCLD_US", params)
            for r in _rows(data.get("output")):
                key = (
                    str(r.get("odno") or ""),
                    str(r.get("pdno") or ""),
                    str(r.get("ord_tmd") or ""),
                )
                if key in seen_us:
                    continue
                seen_us.add(key)
                rows_us.append(r)
        for r in rows_us:
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
