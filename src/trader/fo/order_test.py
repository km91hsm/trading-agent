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


def _place(broker, req: FoOrderRequest, out: dict, what: str):
    """주문 전송. 예외는 오류로 남기고 None(응답 없음이면 주문이 살아 있을 수 있다)."""
    try:
        return broker.place_order(req)
    except Exception as e:  # noqa: BLE001
        out["errors"].append(
            f"{what} 주문 오류(살아 있을 수 있음 — 잔고 확인): {type(e).__name__}: {e}"
        )
        out["unresolved"] = True
        return None


def _close(broker, market, settings, code, kind, close_side, qty, today, *, wait_s, sleep, out):
    """청산: 1차 지정가(옵션, 호가 있을 때) 또는 시장가 → 미체결이면 취소를 **확정**하고 확정된
    잔량만 시장가로 한 번 더. 취소가 확정되지 않으면 재주문하지 않는다(반대 포지션 방지)."""
    attempts: list[dict] = []
    left = qty
    px = None
    if kind != "FUTURES":
        try:
            q2 = market.get_fo_price(code, kind)
            px = q2.bid if close_side == "SELL" else q2.ask
        except Exception as e:  # noqa: BLE001 — 시세 없으면 시장가
            out["errors"].append(f"청산 시세 조회 실패 — 시장가: {type(e).__name__}")
    if px is not None and px > 0:
        req = FoOrderRequest(code=code, side=close_side, qty=left, order_type="LIMIT",
                             price=_limit(settings, kind, px))  # fmt: skip
    else:
        req = FoOrderRequest(code=code, side=close_side, qty=left, order_type="MARKET")
    res = _place(broker, req, out, "청산")
    if res is None:
        return attempts, left
    st = _wait(broker, res.order_no, today, wait_s=wait_s, sleep=sleep)
    filled = st.filled_qty if st is not None else 0
    attempts.append({"order_type": req.order_type, "order_no": res.order_no, "filled": filled})
    if st is not None and st.terminal:
        return attempts, left - filled
    st2 = _cancel_confirm(broker, res.order_no, today, wait_s=wait_s, sleep=sleep)
    if st2 is None or not st2.terminal:
        out["errors"].append(f"청산 주문 {res.order_no} 취소 미확정 — 재주문하지 않음")
        out["unresolved"] = True
        return attempts, left - (st2.filled_qty if st2 is not None else filled)
    left -= st2.filled_qty
    attempts[-1]["filled"] = st2.filled_qty
    if left <= 0:
        return attempts, 0
    mreq = FoOrderRequest(code=code, side=close_side, qty=left, order_type="MARKET")
    mres = _place(broker, mreq, out, "청산 재시도")
    if mres is None:
        return attempts, left
    mst = _wait(broker, mres.order_no, today, wait_s=wait_s, sleep=sleep)
    mf = mst.filled_qty if mst is not None else 0
    attempts.append({"order_type": "MARKET", "order_no": mres.order_no, "filled": mf})
    if mst is None or not mst.terminal:
        out["unresolved"] = True
    return attempts, left - mf


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
    out: dict[str, Any] = {"vehicle": vehicle, "ok": False, "errors": [], "checks": {},
                           "unresolved": False}  # fmt: skip
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
    res = _place(broker, req, out, "진입")
    if res is None:
        return out
    out["entry"] = {"side": side, "order_type": req.order_type,
                    "price": str(req.price) if req.price else None,
                    "order_no": res.order_no}  # fmt: skip
    st = _wait(broker, res.order_no, today, wait_s=wait_s, sleep=sleep)
    if st is None or not st.terminal:
        st2 = _cancel_confirm(broker, res.order_no, today, wait_s=wait_s, sleep=sleep)
        if st2 is None or not st2.terminal:
            out["errors"].append(
                f"진입 주문 {res.order_no} 취소 미확정 — 체결될 수 있어 잔고 확인 필요"
            )
            out["unresolved"] = True
            return out
        st = st2
    filled = st.filled_qty
    out["entry"]["filled"] = filled
    if filled == 0:
        out["errors"].append("진입 미체결 — 취소 확정, 보유 없음")
        return out
    try:  # 확인 단계의 예외가 청산을 막지 않게
        bal = broker.get_balance()
        row = next((p for p in bal.positions if p.code == c.code), None)
        out["checks"] = {
            "row_found": row is not None,
            "contract_side": row is not None and row.contract_side == held_side,
            "kind": row is not None and row.kind == kind,
            "qty": row is not None and row.qty == filled,
            "nav": bal.cash.nav is not None,
            "margin_fields": (
                bal.cash.margin_total is not None and bal.cash.add_margin is not None
            ),
            "unverified_rows": len(bal.unverified) == 0,
        }
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"잔고 확인 실패: {type(e).__name__}: {e}")
        out["checks"] = {"balance": False}
    close_side = "SELL" if side == "BUY" else "BUY"
    attempts, left = _close(broker, market, settings, c.code, kind, close_side, filled, today,
                            wait_s=wait_s, sleep=sleep, out=out)  # fmt: skip
    out["close"] = {"side": close_side, "attempts": attempts, "left": left}
    if left != 0 or out["unresolved"]:
        out["errors"].append(
            f"청산 미확정(남은 {left}계약) — 잔고를 직접 확인하세요(`trader fo verify`)"
        )
    failed = [k for k, v in out["checks"].items() if not v]
    if failed:
        out["errors"].append("확인 실패: " + ", ".join(failed))
    out["ok"] = left == 0 and not out["unresolved"] and not failed
    return out
