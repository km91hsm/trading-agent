"""국내선물옵션 계좌 브로커(Phase 26). 잔고·주문가능·주문·취소·체결·주문 상태.

엄격 파서: 잔고 행은 **방향**(`sll_buy_dvsn_name` ↔ `sll_buy_dvsn_cd`) 과 **종류**(코드 접두 ↔
`prdt_type_cd`) 두 검증을 따로 통과해야 `FoPosition` 이고, 하나라도 실패면 `UnverifiedHolding`(어떤
자동 주문의 대상도 아님). NAV 는 `prsm_dpast_amt` 가 비면 None. 잔고·체결은 연속조회를 완주해야
성공이고 실패·미완주는 예외다. 취소 접수 응답은 확정이 아니며 `get_order_state` 의 계보 판정식만
확정이다. 미확인 값(실기 1 에서 fixture 로 확정): `PRDT_TYPE` 표·현재가 필드·취소 행 표시·
`pdno` 형식.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal

from trader.broker import fo_codes
from trader.broker._parse import dec as _dec
from trader.broker._parse import first_of as _first
from trader.broker._parse import rows as _rows
from trader.broker._parse import to_int as _int
from trader.broker.errors import KisApiError
from trader.broker.kis_client import KisClient
from trader.broker.models import (
    ContractSide,
    FoBalance,
    FoCash,
    FoFill,
    FoKind,
    FoOrderable,
    FoOrderRequest,
    FoOrderState,
    FoPosition,
    OrderResult,
    UnverifiedHolding,
)
from trader.broker.tr_ids import path
from trader.config import KST, AccountConfig, FoProductCfg
from trader.risk.ticks import is_on_tick_table, tick_table_for

logger = logging.getLogger("trader.kis")

# 브로커 응답 계약 버전. 파서가 보는 필드·판정식이 바뀌면 올리고, control `fo_verified:*` 값이
# 이것과
# 다른 수단은 신규 진입이 막힌다(실기 재검증 필요).
CONTRACT_VERSION = 1

# 잔고 `prdt_type_cd` → 종류. 실기 1 에서 확정 전까지는 가정값이고, 표 밖 값은 UnverifiedHolding.
PRDT_TYPE: dict[str, FoKind] = {"301": "FUTURES", "302": "CALL", "303": "PUT"}
SIDE_NAME: dict[str, ContractSide] = {"매수": "LONG", "매도": "SHORT"}
SIDE_CODE: dict[str, ContractSide] = {"02": "LONG", "01": "SHORT"}
CTX200 = ("CTX_AREA_FK200", "CTX_AREA_NK200")


def _side_of(r: dict) -> tuple[ContractSide | None, str | None]:
    """방향 검증: 이름·코드 둘 다 있으면 일치해야 하고, 하나만 있으면 알려진 값이어야 한다."""
    name = str(r.get("sll_buy_dvsn_name") or "").strip()
    code = str(r.get("sll_buy_dvsn_cd") or "").strip()
    by_name = SIDE_NAME.get(name)
    by_code = SIDE_CODE.get(code)
    if name and code:
        if by_name is None or by_code is None or by_name != by_code:
            return None, f"방향 불일치(name={name!r}, cd={code!r})"
        return by_name, None
    if name:
        return (by_name, None) if by_name else (None, f"방향 미지(name={name!r})")
    if code:
        return (by_code, None) if by_code else (None, f"방향 미지(cd={code!r})")
    return None, "방향 없음"


def _kind_of(r: dict, code: str, product: FoProductCfg) -> tuple[FoKind | None, str | None]:
    """종류 검증: 코드 접두(상품 설정) ↔ `prdt_type_cd` 일치."""
    by_prefix = fo_codes.kind_of(code, product)
    type_cd = str(r.get("prdt_type_cd") or "").strip()
    by_type = PRDT_TYPE.get(type_cd)
    if by_prefix is None:
        return None, f"종류 미지(접두 {code[:3]!r})"
    if by_type is None:
        return None, f"종류 미지(prdt_type_cd={type_cd!r})"
    if by_prefix != by_type:
        return None, f"종류 불일치(접두 {by_prefix}, prdt_type_cd {type_cd!r})"
    return by_prefix, None


def _qty_of(raw: Any) -> tuple[int | None, str | None]:
    """잔고 수량의 엄격 해석: 음이 아닌 정수 문자열만 수량, 그 외(결측·비정수·음수)는 사유를 돌려
    미확정 보유로 남긴다 — `_int` 의 '해석 불가 → 0' 폴백이 보유 행을 조용히 지우지 않게
    (Codex P0-1)."""
    s = str(raw).strip() if raw is not None else ""
    if not s.isdecimal():  # isdigit 은 '²' 같은 비십진 문자도 True 라 int() 가 터진다
        return None, f"수량 해석 불가: {s!r}"
    return int(s), None


def _req_int(row: dict, key: str, order_no: str) -> int:
    """체결 응답의 필수 수량. 결측·비정수는 '완료 0' 이 아니라 예외(Codex P0-2)."""
    s = str(row.get(key) or "").strip()
    if not s.isdecimal():
        raise KisApiError("INVALID_JSON", f"체결 응답 수량 누락({key}): {order_no}", "FO_CCLD")
    return int(s)


def _local_dt(ymd: Any, hms: Any) -> datetime:
    s = f"{ymd or ''}{str(hms or '000000').zfill(6)}"
    try:
        local = datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=KST)
    except ValueError:
        local = datetime.strptime(str(ymd), "%Y%m%d").replace(tzinfo=KST)
    return local.astimezone(UTC)


def _is_cancel_row(r: dict) -> bool:
    """정정취소 행 판정(취소). 필드명은 미확인이라 후보를 모두 본다."""
    if str(r.get("rvse_cncl_dvsn_cd") or "").strip() == "02":
        return True
    name = str(_first(r, "rvse_cncl_dvsn_name", "trad_dvsn_name", "ord_dvsn_name") or "")
    return "취소" in name


class KisFoBroker:
    def __init__(
        self,
        account: AccountConfig,
        client: KisClient,
        *,
        product: FoProductCfg,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if account.asset != "fo":
            raise ValueError(f"계좌 '{account.id}' 는 asset fo 가 아닙니다")
        if client.env != account.env:
            raise ValueError(
                f"계좌 '{account.id}' env({account.env})와 클라이언트 env({client.env}) 불일치"
            )
        self.account = account
        self._client = client
        self.product = product
        self._now = now

    @property
    def client(self) -> KisClient:
        return self._client

    def _acct(self) -> dict[str, str]:
        return {"CANO": self.account.cano, "ACNT_PRDT_CD": self.account.acnt_prdt_cd}

    # --- 잔고 ------------------------------------------------------------------------

    def get_balance(self, *, mgna_dvsn: str = "01", excc_stat_cd: str = "1") -> FoBalance:
        """잔고(output1 행 → 포지션/미확정, output2 → 현금). 연속조회 완주 실패·output2 없음은
        예외."""
        requested_at = self._now()
        params = {
            **self._acct(),
            "MGNA_DVSN": mgna_dvsn,
            "EXCC_STAT_CD": excc_stat_cd,
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }
        data = self._client.get_pages(
            path("FO_BALANCE"), "FO_BALANCE", params, ctx_keys=CTX200, strict=True
        )
        received_at = self._now()
        summary_rows = _rows(data.get("output2"))
        if not summary_rows:
            raise KisApiError(
                "INVALID_JSON", "선물옵션 잔고 응답에 output2 가 없습니다", "FO_BALANCE"
            )
        s = summary_rows[0]
        positions: list[FoPosition] = []
        unverified: list[UnverifiedHolding] = []
        for r in _rows(data.get("output1")):
            code = fo_codes.normalize(_first(r, "shtn_pdno", "pdno") or "")
            qty, why0 = _qty_of(r.get("cblc_qty"))
            if qty == 0 and why0 is None:
                continue  # 명시적인 정수 0 만 '보유 없음'
            side, why = _side_of(r)
            kind, why2 = _kind_of(r, code, self.product)
            reasons = [w for w in (why0, why, why2) if w]
            if reasons or side is None or kind is None or qty is None:
                unverified.append(
                    UnverifiedHolding(code=code, raw=dict(r), reason="; ".join(reasons))
                )
                continue
            strike = None
            if kind != "FUTURES":
                raw = _first(r, "acpr", "strike")
                strike = _dec(raw) if raw is not None else _strike_from_name(r.get("prdt_name"))
            positions.append(
                FoPosition(
                    code=code,
                    name=str(r.get("prdt_name") or "").strip() or None,
                    kind=kind,
                    contract_side=side,
                    qty=qty,  # type: ignore[arg-type]
                    avg_price=_dec(r.get("ccld_avg_unpr1")),
                    current_price=_dec(_first(r, "prpr", "excc_unpr") or "0"),
                    pnl=_dec(r.get("evlu_pfls_amt")),
                    sellable_qty=_int(r.get("lqd_psbl_qty")),
                    strike=strike,
                )
            )
        nav_raw = s.get("prsm_dpast_amt")
        cash = FoCash(
            deposit=_dec(_first(s, "tot_dncl_amt", "dnca_cash") or "0"),
            orderable_cash=_dec(s.get("ord_psbl_cash")),
            orderable_total=_dec(s.get("ord_psbl_tota")),
            margin_total=_dec(s.get("mgna_tota")),
            add_margin=_dec(s.get("add_mgna_tota")),
            nav=_dec(nav_raw) if nav_raw not in (None, "") else None,
            realized_today=_dec(s.get("trad_pfls_amt_smtl")),
            unrealized=_dec(s.get("evlu_pfls_amt_smtl")),
            requested_at=requested_at,
            received_at=received_at,
        )
        return FoBalance(positions=positions, unverified=unverified, cash=cash)

    # --- 주문가능 ---------------------------------------------------------------------

    def orderable(
        self,
        code: str,
        side: Literal["BUY", "SELL"],
        price: Decimal | None,
        order_type: Literal["LIMIT", "MARKET"],
    ) -> FoOrderable:
        """주문가능 수량. 응답에 현금 필드가 없으면 `cash=None`(호출자는 잔고의 주문가능현금을
        쓴다)."""
        params = {
            **self._acct(),
            "PDNO": fo_codes.normalize(code),
            "SLL_BUY_DVSN_CD": "02" if side == "BUY" else "01",
            "UNIT_PRICE": _price_str(price) if order_type == "LIMIT" else "0",
            "ORD_DVSN_CD": "01" if order_type == "LIMIT" else "02",
        }
        data = self._client.get(path("FO_PSBL_ORDER"), "FO_PSBL_ORDER", params)
        out = data.get("output") or {}
        cash_raw = _first(out, "ord_psbl_cash", "ord_psbl_amt")
        idx_raw = out.get("bass_idx")
        return FoOrderable(
            tot_qty=_int(out.get("tot_psbl_qty")),
            lqd_qty=_int(out.get("lqd_psbl_qty1")),
            ord_qty=_int(out.get("ord_psbl_qty")),
            cash=_dec(cash_raw) if cash_raw is not None else None,
            bass_idx=_dec(idx_raw) if idx_raw not in (None, "") else None,
        )

    # --- 주문·취소 --------------------------------------------------------------------

    def place_order(self, req: FoOrderRequest, *, night: bool = False) -> OrderResult:
        """주간 주문(모의 야간 TR 없음 → UNSUPPORTED). 지정가는 tick 표로 검증한 뒤 보낸다."""
        code = fo_codes.normalize(req.code)
        kind = fo_codes.kind_of(code, self.product)
        if kind is None:
            raise ValueError(f"상품 {self.product.name} 의 코드가 아닙니다: {code}")
        if req.order_type == "LIMIT":
            if req.price is None:
                raise ValueError("지정가 주문에는 가격이 필요합니다")
            if not is_on_tick_table(tick_table_for(self.product, kind), req.price):
                raise ValueError(f"호가단위 밖 지정가 {req.price} ({code})")
            unit = _price_str(req.price)
        else:
            unit = "0"
        name = "FO_ORDER_NIGHT" if night else "FO_ORDER"
        body = {
            "ORD_PRCS_DVSN_CD": "02",
            **self._acct(),
            "SLL_BUY_DVSN_CD": "02" if req.side == "BUY" else "01",
            "SHTN_PDNO": code,
            "ORD_QTY": str(req.qty),
            "UNIT_PRICE": unit,
            "NMPR_TYPE_CD": "01" if req.order_type == "LIMIT" else "02",
            "KRX_NMPR_CNDT_CD": "0",
            "ORD_DVSN_CD": "01" if req.order_type == "LIMIT" else "02",
            "CTAC_TLNO": "",
            "FUOP_ITEM_DVSN_CD": "",
        }
        data = self._client.post(path(name), name, body)
        out = data.get("output") or {}
        return OrderResult(
            order_no=str(out.get("ODNO") or "") or None,
            accepted=True,
            message=str(data.get("msg1") or ""),
        )

    def cancel_order(self, order_no: str, *, qty: int = 0, night: bool = False) -> OrderResult:
        """취소 **접수**(확정 아님 — `get_order_state` 로 확정). qty 0 이면 잔량 전부."""
        name = "FO_ORDER_CANCEL_NIGHT" if night else "FO_ORDER_CANCEL"
        body = {
            "ORD_PRCS_DVSN_CD": "02",
            **self._acct(),
            "RVSE_CNCL_DVSN_CD": "02",
            "ORGN_ODNO": order_no,
            "ORD_QTY": str(qty),
            "UNIT_PRICE": "0",
            "NMPR_TYPE_CD": "01",
            "KRX_NMPR_CNDT_CD": "0",
            "RMN_QTY_YN": "Y" if qty == 0 else "N",
            "ORD_DVSN_CD": "01",
            "FUOP_ITEM_DVSN_CD": "",
        }
        data = self._client.post(path(name), name, body)
        out = data.get("output") or {}
        return OrderResult(
            order_no=str(out.get("ODNO") or "") or order_no,
            accepted=True,
            message=str(data.get("msg1") or ""),
        )

    # --- 체결·주문 상태 --------------------------------------------------------------

    def _ccnl_rows(self, d: date) -> list[dict]:
        ymd = d.strftime("%Y%m%d")
        params = {
            **self._acct(),
            "STRT_ORD_DT": ymd,
            "END_ORD_DT": ymd,
            "SLL_BUY_DVSN_CD": "00",
            "CCLD_NCCS_DVSN": "00",
            "SORT_SQN": "AS",
            "PDNO": "",
            "STRT_ODNO": "",
            "MKET_ID_CD": "",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }
        data = self._client.get_pages(
            path("FO_CCLD"), "FO_CCLD", params, ctx_keys=CTX200, strict=True
        )
        return _rows(data.get("output1"))

    def get_fills(self, d: date) -> list[FoFill]:
        """그날 주문 행(누적 체결 > 0). `filled_qty=tot_ccld_qty`(누적), `remaining_qty=qty`
        (잔량)."""
        out: list[FoFill] = []
        for r in self._ccnl_rows(d):
            filled = _int(r.get("tot_ccld_qty"))
            if filled <= 0:
                continue
            orgn = str(r.get("orgn_odno") or "").strip() or None
            amt = _dec(r.get("tot_ccld_amt"))
            avg_raw = _first(r, "avg_idx", "ccld_avg_unpr")
            out.append(
                FoFill(
                    order_no=str(r.get("odno") or ""),
                    orgn_order_no=orgn,
                    code=fo_codes.normalize(_first(r, "shtn_pdno", "pdno") or ""),
                    side="SELL" if str(r.get("sll_buy_dvsn_cd")) == "01" else "BUY",
                    remaining_qty=_int(r.get("qty")),
                    filled_qty=filled,
                    rejected_qty=_int(r.get("rjct_qty")),
                    cancelled_qty=0,
                    filled_amount=amt,
                    avg_price=_dec(avg_raw) if avg_raw is not None else None,
                    filled_at=_local_dt(r.get("ord_dt"), r.get("ord_tmd")),
                )
            )
        return out

    def get_order_state(self, order_no: str, d: date) -> FoOrderState:
        """원주문 계보의 확정 상태. 원주문 행이 없으면 `terminal=False, open`(unknown 유지).
        필수 수량 결측·항등식 불일치(체결+거부+취소+잔량 ≠ 주문수량)는 예외 — 완료로 보지 않는다."""
        rows = self._ccnl_rows(d)
        orig = next((r for r in rows if str(r.get("odno") or "") == order_no), None)
        if orig is None:
            return FoOrderState(
                order_no=order_no, order_qty=0, filled_qty=0, remaining_qty=0, rejected_qty=0,
                cancelled_qty=0, terminal=False, state="open",
            )
        order_qty = _req_int(orig, "ord_qty", order_no)
        filled = _req_int(orig, "tot_ccld_qty", order_no)
        remaining = _req_int(orig, "qty", order_no)
        rejected = _req_int(orig, "rjct_qty", order_no)
        cancel_rows = [
            r for r in rows
            if str(r.get("orgn_odno") or "") == order_no and _is_cancel_row(r)
        ]
        cancelled = 0
        if cancel_rows:
            cancelled = order_qty - filled - rejected - remaining
            if cancelled < 0:
                raise KisApiError(
                    "INVALID_JSON", f"체결 응답 모순(취소 수량 음수): {order_no}", "FO_CCLD"
                )
        if filled + rejected + cancelled + remaining != order_qty:
            raise KisApiError(
                "INVALID_JSON",
                f"체결 응답 모순(체결+거부+취소+잔량 ≠ 주문수량): {order_no}",
                "FO_CCLD",
            )
        if cancel_rows and remaining == 0:
            state = "cancelled"
        elif remaining == 0 and rejected > 0 and filled == 0:
            state = "rejected"
        elif remaining == 0:
            state = "filled"
        else:
            state = "open"
        terminal = (remaining == 0 or state in ("cancelled", "rejected")) and (
            filled + rejected + cancelled == order_qty
        )
        return FoOrderState(
            order_no=order_no,
            order_qty=order_qty,
            filled_qty=filled,
            remaining_qty=remaining,
            rejected_qty=rejected,
            cancelled_qty=cancelled,
            terminal=terminal,
            state=state,  # type: ignore[arg-type]
        )


def _strike_from_name(name: Any) -> Decimal | None:
    """종목명 끝의 행사가(예 '… C 202611 352.5'). 없으면 None."""
    parts = str(name or "").split()
    if not parts:
        return None
    try:
        return Decimal(parts[-1])
    except Exception:  # noqa: BLE001
        return None


def _price_str(price: Decimal | None) -> str:
    if price is None:
        return "0"
    return format(price.normalize(), "f")
