"""실기 2 왕복 검증(Phase 26, `trader fo order-test`, 거부 목록 — 사용자만 실행). 모의 선물옵션
계좌에서 수단 하나를 **1계약** 진입 → 체결 확정 → 잔고 행으로 방향·종류·NAV·증거금 필드 확인 →
즉시 청산 → 체결 확정. 전부 통과해야 호출자가 그 수단의 `fo_verified` 플래그를 연다.

원장(`fo_positions`·`orders`)에는 적지 않는다(현물 `kis order-test` 와 같다). 진입이 정해진
시간 안에 체결되지 않으면 취소를 확정하고 실패로 끝낸다(남은 보유가 생기지 않게). 청산 지정가가
안 맞으면 취소 확정 뒤 시장가로 한 번 더 낸다."""

from __future__ import annotations

import time as time_mod
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

from trader.broker import fo_master
from trader.broker.models import FoOrderRequest, FoOrderState
from trader.config import Settings
from trader.risk.ticks import quantize_tick, tick_table_for

ENTRY = {  # 수단 → (계약 종류, 진입 방향, 기대 보유 방향)
    "FUTURES": ("FUTURES", "BUY", "LONG"),
    "OPTION_BUY": ("CALL", "BUY", "LONG"),
    "OPTION_SELL": ("CALL", "SELL", "SHORT"),
}


def pick_contract(
    master: list[fo_master.Contract],
    settings: Settings,
    vehicle: str,
    *,
    index: Decimal,
    today: date,
) -> fo_master.Contract | None:
    """선물은 최근월, 옵션 매수는 ATM 콜, 옵션 매도는 ATM 바로 위(OTM1) 콜."""
    pcfg = settings.fo.product_cfg()
    roll = settings.fo.roll_days

    def no_quote(_: fo_master.Contract) -> date | None:
        return None

    if vehicle == "FUTURES":
        return fo_master.front_month(master, pcfg, "FUTURES", today=today, roll_days=roll,
                                     last_trade_date_of=no_quote)  # fmt: skip
    _, cs = fo_master.option_front(master, pcfg, today=today, roll_days=roll + 1,
                                   last_trade_date_of=no_quote)  # fmt: skip
    calls = sorted((c for c in cs if c.kind == "CALL" and c.strike is not None),
                   key=lambda c: c.strike)  # fmt: skip
    if not calls:
        return None
    atm = min(calls, key=lambda c: abs(c.strike - index))
    if vehicle == "OPTION_BUY":
        return atm
    above = [c for c in calls if c.strike > atm.strike]
    return above[0] if above else None


def _wait(
    broker: Any, order_no: str, today: date, *, wait_s: float, sleep: Callable[[float], None]
) -> FoOrderState | None:
    deadline = time_mod.monotonic() + wait_s
    st = None
    while True:
        try:
            st = broker.get_order_state(order_no, today)
        except Exception:  # noqa: BLE001 — 조회 실패는 다음 폴링에서 다시
            st = None
        if st is not None and st.terminal:
            return st
        if time_mod.monotonic() >= deadline:
            return st
        sleep(1.0)


def _cancel_confirm(broker, order_no, today, *, wait_s, sleep) -> FoOrderState | None:
    try:
        broker.cancel_order(order_no, qty=0)
    except Exception:  # noqa: BLE001 — 취소 거부여도 상태 조회로 확정
        pass
    return _wait(broker, order_no, today, wait_s=wait_s, sleep=sleep)


def _limit(settings: Settings, kind: str, price: Decimal) -> Decimal:
    return quantize_tick(tick_table_for(settings.fo.product_cfg(), kind), price)


def run(
    broker: Any,
    market: Any,
    master: list[fo_master.Contract],
    settings: Settings,
    vehicle: str,
    *,
    today: date,
    wait_s: float = 30.0,
    sleep: Callable[[float], None] = time_mod.sleep,
) -> dict[str, Any]:
    kind, side, held_side = ENTRY[vehicle]
    out: dict[str, Any] = {"vehicle": vehicle, "ok": False, "errors": [], "checks": {}}
    index, _ = market.get_kospi200()
    c = pick_contract(master, settings, vehicle, index=index, today=today)
    if c is None:
        out["errors"].append("계약을 고르지 못했습니다(마스터·월물)")
        return out
    out["code"] = c.code
    q = market.get_fo_price(c.code, kind)
    if vehicle == "FUTURES":
        req = FoOrderRequest(code=c.code, side=side, qty=1, order_type="MARKET")
    else:  # 옵션은 지정가 진입(매수 ask, 매도 bid)
        px = q.ask if side == "BUY" else q.bid
        if px is None or px <= 0:
            out["errors"].append("호가 없음 — 옵션 진입 지정가를 정할 수 없습니다")
            return out
        req = FoOrderRequest(code=c.code, side=side, qty=1, order_type="LIMIT",
                             price=_limit(settings, kind, px))  # fmt: skip
    res = broker.place_order(req)
    out["entry"] = {"side": side, "order_type": req.order_type,
                    "price": str(req.price) if req.price else None,
                    "order_no": res.order_no}  # fmt: skip
    st = _wait(broker, res.order_no, today, wait_s=wait_s, sleep=sleep)
    if st is None or not st.terminal or st.filled_qty != 1:
        st2 = _cancel_confirm(broker, res.order_no, today, wait_s=wait_s, sleep=sleep)
        filled = (st2 or st).filled_qty if (st2 or st) is not None else 0
        out["entry"]["filled"] = filled
        if filled == 0:
            out["errors"].append("진입 미체결 — 취소 확정, 보유 없음")
            return out
        st = st2
    out["entry"]["filled"] = st.filled_qty if st else None
    bal = broker.get_balance()
    row = next((p for p in bal.positions if p.code == c.code), None)
    out["checks"] = {
        "row_found": row is not None,
        "contract_side": row is not None and row.contract_side == held_side,
        "kind": row is not None and row.kind == kind,
        "qty": row is not None and row.qty == 1,
        "nav": bal.cash.nav is not None,
        "margin_fields": bal.cash.margin_total is not None and bal.cash.add_margin is not None,
        "unverified_rows": len(bal.unverified) == 0,
    }
    # 청산(확인 결과와 무관하게 반드시 시도 — 잔고에 남기지 않는다)
    close_side = "SELL" if side == "BUY" else "BUY"
    q2 = market.get_fo_price(c.code, kind)
    if vehicle == "FUTURES":
        creq = FoOrderRequest(code=c.code, side=close_side, qty=1, order_type="MARKET")
    else:
        px = q2.bid if close_side == "SELL" else q2.ask
        creq = (
            FoOrderRequest(code=c.code, side=close_side, qty=1, order_type="LIMIT",
                           price=_limit(settings, kind, px))  # fmt: skip
            if px is not None and px > 0
            else FoOrderRequest(code=c.code, side=close_side, qty=1, order_type="MARKET")
        )
    cres = broker.place_order(creq)
    cst = _wait(broker, cres.order_no, today, wait_s=wait_s, sleep=sleep)
    attempts = [{"order_type": creq.order_type, "order_no": cres.order_no,
                 "filled": cst.filled_qty if cst else None}]  # fmt: skip
    if (cst is None or cst.filled_qty < 1) and creq.order_type == "LIMIT":
        _cancel_confirm(broker, cres.order_no, today, wait_s=wait_s, sleep=sleep)
        mreq = FoOrderRequest(code=c.code, side=close_side, qty=1, order_type="MARKET")
        mres = broker.place_order(mreq)
        cst = _wait(broker, mres.order_no, today, wait_s=wait_s, sleep=sleep)
        attempts.append({"order_type": "MARKET", "order_no": mres.order_no,
                         "filled": cst.filled_qty if cst else None})  # fmt: skip
    out["close"] = {"side": close_side, "attempts": attempts}
    closed = cst is not None and cst.filled_qty == 1
    if not closed:
        out["errors"].append("청산 미확정 — 잔고를 직접 확인하세요(`trader fo verify`)")
    failed = [k for k, v in out["checks"].items() if not v]
    if failed:
        out["errors"].append("확인 실패: " + ", ".join(failed))
    out["ok"] = closed and not failed
    return out
