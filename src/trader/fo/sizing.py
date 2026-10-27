"""파생 사이징·손실 예산·시나리오 스트레스·증거금 버퍼(Phase 26, 스펙 §4.5~§4.7) — 순수 함수.

- 계획 손실(planned loss)은 손절선까지의 추정치일 뿐 갭·호가 공백·거부로 보장되지 않는다.
- 수명 손실 예산: `실현손실 + 비용 + 보유 계획 손실 + 미체결·이번 다리 계획 손실 ≤ nav_at_open × r`.
  부분 청산·증액이 예산을 리셋하지 않고 재충전은 없다(새 포지션 id 가 새 예산).
- 옵션 보호선 `premium_stop` 은 절대 프리미엄이며 단조(매도 min·매수 max)다. 모니터·계획 손실은
  이 값으로만 계산한다.
- 증거금: 계약당 필요증거금의 검증된 산식이 없으므로 브로커 주문가능 응답(현금 C 로 q 계약 가능)
  에서만 `n ≤ floor(q × (1 − buffer))` 를 끌어낸다. 전제(상계 없음·tot == ord·lqd == 0·q ≥ 1·C 있음)
  가 깨지면 선물·옵션 매도 신규 거부. 공표 증거금률은 상한이 아니라 정합성 검사에만 쓴다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_DOWN, Decimal

from trader.broker.models import FoOrderable, FoQuote
from trader.config import FoStressCfg
from trader.fo import pricing
from trader.fo.types import FoLegRow, FoPositionRow, GateInput, SizeResult, is_entry

_ZERO = Decimal(0)


def _d(v: float | Decimal | int | str) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


def stop_fut_of(
    stop_pt: Decimal, basis: Decimal | None, direction: str, slippage_pt: Decimal
) -> Decimal:
    """선물 손절 가격 = 손절선 + 베이시스 − 방향부호 × 슬리피지(보수적: 롱은 더 낮게, 숏은 더
    높게)."""
    sign = 1 if direction == "LONG" else -1
    return stop_pt + (basis or _ZERO) - sign * slippage_pt


def planned_loss_per_contract(
    vehicle: str,
    kind: str,
    *,
    ref_price: Decimal,
    stop_fut: Decimal | None,
    premium_stop: Decimal | None,
    multiplier: int,
) -> Decimal:
    """계약 1개의 계획 손실(≥ 0). 선물 |기준가 − 손절가|, 옵션 매수 (기준가 − 보호선), 옵션 매도
    (보호선 − 기준가). 입력이 없으면 선물은 ValueError, 옵션 매수는 전액(기준가)."""
    m = Decimal(multiplier)
    if vehicle == "FUTURES":
        if stop_fut is None:
            raise ValueError("선물 계획 손실에는 stop_fut 이 필요합니다")
        return abs(ref_price - stop_fut) * m
    if vehicle == "OPTION_BUY":
        if premium_stop is None:
            return ref_price * m
        return max(ref_price - premium_stop, _ZERO) * m
    if premium_stop is None:
        raise ValueError("옵션 매도 계획 손실에는 premium_stop 이 필요합니다")
    return max(premium_stop - ref_price, _ZERO) * m


def premium_stop_candidate(
    vehicle: str, avg_premium: Decimal, *, approved_mult: float | None, buy_stop_pct: float
) -> Decimal:
    """후보 보호선: 매도 = 평균가 × 배수, 매수 = 평균가 × (1 − buy_stop_pct)."""
    if vehicle == "OPTION_SELL":
        if approved_mult is None:
            raise ValueError("옵션 매도 보호선에는 approved_mult 가 필요합니다")
        return avg_premium * _d(approved_mult)
    return avg_premium * (Decimal(1) - _d(buy_stop_pct))


def premium_stop_merge(existing: Decimal | None, candidate: Decimal, vehicle: str) -> Decimal:
    """단조 병합: 매도는 느슨해질 수 없고(min), 매수도 느슨해질 수 없다(max)."""
    if existing is None:
        return candidate
    return min(existing, candidate) if vehicle == "OPTION_SELL" else max(existing, candidate)


def lifetime_budget(
    position: FoPositionRow | None,
    *,
    nav_at_open: Decimal,
    r: float,
    held_loss: Decimal,
    pending_loss: Decimal,
) -> dict:
    """수명 예산 집계. `ok` 는 사용 + 계획 ≤ 한도."""
    used = (position.realized_loss + position.fees) if position is not None else _ZERO
    limit = nav_at_open * _d(r)
    planned = held_loss + pending_loss
    return {"used": used, "planned": planned, "limit": limit, "ok": used + planned <= limit}


@dataclass(frozen=True)
class StressItem:
    quote: FoQuote
    signed_qty: int  # 매수 +, 매도 −
    days_left: int


def stress_loss(items: list[StressItem], *, cfg: FoStressCfg, multiplier: int) -> Decimal:
    """격자(지수 ±index_pct × IV 상대 변화)의 최악 손실(≥ 0). 선물 선형·옵션 Black-76 재평가."""
    worst = _ZERO
    m = Decimal(multiplier)
    for s in cfg.index_pct:
        for sign in (1.0, -1.0):
            for v in cfg.iv_rel:
                pnl = _ZERO
                for it in items:
                    new = pricing.reprice(
                        it.quote, index_shift_pct=sign * s, iv_rel=v, days_left=it.days_left
                    )
                    pnl += Decimal(it.signed_qty) * (new - it.quote.price) * m
                worst = max(worst, -pnl)
    return worst


def local_reserved(legs: list[FoLegRow], *, per_contract: Decimal) -> Decimal:
    """로컬 예약 현금 = 살아 있는 다리(prepared·sending·unknown + accepted 미체결) 잔량 × 계약당
    증거금 추정. 체결분은 체결 뒤에 시작한 잔고 응답이 보여 줄 때 보유로 옮긴다(호출자)."""
    return sum((Decimal(x.remaining) * per_contract for x in legs if x.live), _ZERO)


def margin_check(
    *,
    n: int,
    orderable: FoOrderable | None,
    nav: Decimal,
    local_reserved: Decimal,
    buffer_pct: float,
    add_margin: Decimal,
    notional: Decimal,
    margin_rate: float | None,
    rate_asof: date,
    max_age_days: int,
    today: date,
) -> tuple[int, list[str]]:
    """허용 신규 계약 수와 거부 사유(전부 수집). 사유가 있으면 0."""
    reasons: list[str] = []
    if orderable is None:
        return 0, ["주문가능 조회 없음"]
    cash, q = orderable.cash, orderable.ord_qty
    if cash is None:
        reasons.append("주문가능 응답에 기준 현금 없음")
    if q < 1:
        reasons.append("주문가능 수량 0")
    if orderable.tot_qty != q:
        reasons.append(f"청산가능분 섞임(tot {orderable.tot_qty} ≠ ord {q})")
    if orderable.lqd_qty != 0:
        reasons.append(f"청산가능 수량 {orderable.lqd_qty} > 0(상계 전제 깨짐)")
    if add_margin > 0:
        reasons.append(f"추가 증거금 {add_margin}")
    if (today - rate_asof).days > max_age_days:
        reasons.append(f"증거금률 노후(asof {rate_asof.isoformat()})")
    if reasons:
        return 0, reasons
    assert cash is not None
    buf = _d(buffer_pct)
    if cash - local_reserved < nav * buf:
        reasons.append(f"현금 버퍼 미달: {cash} − 예약 {local_reserved} < NAV × {buffer_pct}")
    per = cash / Decimal(q)
    if margin_rate is not None and per > notional * _d(margin_rate) * 3:
        reasons.append(f"주문가능 응답 정합성 실패(계약당 {per:.0f} > 명목×증거금률×3)")
    if reasons:
        return 0, reasons
    allowed = int(math.floor(q * (1 - buffer_pct)))
    return min(n, allowed), []


def _floor(x: Decimal) -> int:
    return int(x.to_integral_value(rounding=ROUND_DOWN)) if x > 0 else 0


def size(
    gi: GateInput,
    *,
    L: float,
    approved_mult: float | None,
    premium_stop: Decimal | None,
    stop_pt: Decimal,
    ref_price: Decimal,
) -> SizeResult:
    """총량 기준 사이징. 전제(시세·기초지수·NAV 유효)는 `rules.evaluate` 가 먼저 검사한다.
    n_target = floor(NAV × L ÷ 델타명목/계약) 을 계약 수·옵션 전액/명목·수명 예산·스트레스·증거금
    버퍼로 차례로 자르고, 이번 다리 = n_target − 보유 − 살아 있는 진입 다리."""
    d, cfg, port = gi.decision, gi.cfg, gi.port
    pcfg = cfg.product_cfg()
    mult = pcfg.multiplier
    pos = gi.position
    q = gi.quote
    assert q is not None and gi.underlying is not None and port.nav is not None
    nav = port.nav
    reasons: list[str] = []
    notes: list[str] = []
    delta = 1.0 if d.vehicle == "FUTURES" else abs(float(q.delta or 0.0))
    dn = _d(delta) * gi.underlying * Decimal(mult)  # 델타명목/계약
    if dn <= 0:
        return SizeResult(0, 0, _ZERO, _ZERO, _ZERO, _ZERO, {}, ref_price, ["델타명목 0"])
    held = pos.qty_held if pos else 0
    live = sum(
        x.remaining
        for x in port.legs
        if pos is not None and x.position_id == pos.id and is_entry(x.intent) and x.live
    )
    others = sum(p.qty_held for p in port.positions if pos is None or p.id != pos.id) + sum(
        x.remaining
        for x in port.legs
        if is_entry(x.intent) and x.live and (pos is None or x.position_id != pos.id)
    )
    n_target = _floor(nav * _d(L) / dn)
    cap = cfg.max_contracts - others
    if n_target > cap:
        notes.append(f"계약 수 한도로 {n_target}→{max(cap, 0)}")
        n_target = max(cap, 0)
    if d.vehicle == "OPTION_BUY":
        cap_prem = _floor(nav * _d(cfg.option.max_premium_pct) / (ref_price * Decimal(mult)))
        if n_target > cap_prem:
            notes.append(f"옵션 매수 전액 한도로 {n_target}→{cap_prem}")
            n_target = cap_prem
    elif d.vehicle == "OPTION_SELL":
        cap_not = _floor(
            nav * _d(cfg.option.sell_max_notional_pct) / (gi.underlying * Decimal(mult))
        )
        if n_target > cap_not:
            notes.append(f"옵션 매도 명목 한도로 {n_target}→{cap_not}")
            n_target = cap_not
    # 계획 손실/계약
    stop_fut = (
        stop_fut_of(stop_pt, q.basis, d.direction, _d(cfg.slippage_pt))
        if d.vehicle == "FUTURES"
        else None
    )
    per_loss = planned_loss_per_contract(
        d.vehicle, q.kind, ref_price=ref_price, stop_fut=stop_fut,
        premium_stop=premium_stop, multiplier=mult,
    )  # fmt: skip
    nav_at_open = pos.nav_at_open if pos is not None and pos.nav_at_open is not None else nav
    budget = lifetime_budget(
        pos, nav_at_open=nav_at_open, r=cfg.risk_per_trade_pct,
        held_loss=per_loss * held, pending_loss=per_loss * live,
    )  # fmt: skip
    if per_loss > 0:
        room = budget["limit"] - budget["used"]
        n_budget = _floor(room / per_loss)
        if n_budget < held + live:
            reasons.append(
                f"수명 손실 예산 초과: 사용 {budget['used']} + 계획 {budget['planned']} "
                f"> 한도 {budget['limit']}"
            )
        if n_target > n_budget:
            notes.append(f"수명 손실 예산으로 {n_target}→{max(n_budget, 0)}")
            n_target = max(n_budget, 0)
    elif d.vehicle == "FUTURES":
        reasons.append("선물 계획 손실 0(손절선이 기준가와 같음)")
    # 시나리오 스트레스(같은 계약이라 계약 수에 선형)
    if d.vehicle == "FUTURES":
        sign = 1 if d.direction == "LONG" else -1
    elif d.vehicle == "OPTION_BUY":
        sign = 1
    else:
        sign = -1
    try:
        per_stress = stress_loss(
            [StressItem(q, sign, gi.days_left or 0)], cfg=cfg.stress, multiplier=mult
        )
    except ValueError as e:
        return SizeResult(
            0, 0, _ZERO, per_loss, _ZERO, _ZERO, budget, ref_price, [f"스트레스 재평가 불가: {e}"]
        )
    if cfg.hold_overnight and per_stress > 0:
        n_stress = _floor(nav * _d(cfg.overnight_risk_pct) / per_stress)
        if n_stress < held + live:
            reasons.append(f"시나리오 최악 손실이 NAV × {cfg.overnight_risk_pct} 초과(보유분)")
        if n_target > n_stress:
            notes.append(f"스트레스 한도로 {n_target}→{max(n_stress, 0)}")
            n_target = max(n_stress, 0)
    n_leg = n_target - held - live
    # 증거금 버퍼(선물·옵션 매도, 신규 다리가 있을 때만)
    if n_leg > 0 and d.vehicle in ("FUTURES", "OPTION_SELL"):
        rate = cfg.margin_rate.futures if d.vehicle == "FUTURES" else cfg.margin_rate.option_sell
        per_margin = (
            gi.orderable.cash / Decimal(gi.orderable.ord_qty)
            if gi.orderable is not None
            and gi.orderable.cash is not None
            and gi.orderable.ord_qty > 0
            else _ZERO
        )
        allowed, mreasons = margin_check(
            n=n_leg,
            orderable=gi.orderable,
            nav=nav,
            local_reserved=local_reserved(port.legs, per_contract=per_margin),
            buffer_pct=cfg.margin_buffer_pct,
            add_margin=port.cash.add_margin if port.cash is not None else _ZERO,
            notional=gi.underlying * Decimal(mult),
            margin_rate=rate,
            rate_asof=cfg.margin_rate.asof,
            max_age_days=cfg.margin_rate.max_age_days,
            today=gi.today,
        )
        reasons.extend(mreasons)
        if not mreasons and allowed < n_leg:
            notes.append(f"증거금 버퍼로 다리 {n_leg}→{allowed}")
            n_leg = allowed
            n_target = held + live + n_leg
    total = held + live + max(n_leg, 0)
    budget["planned"] = per_loss * total
    budget["ok"] = budget["used"] + budget["planned"] <= budget["limit"]
    return SizeResult(
        n_target=n_target,
        n_leg=n_leg,
        planned_loss_leg=per_loss * max(n_leg, 0),
        planned_loss_per_contract=per_loss,
        exposure_after=dn * total,
        stress_loss_after=per_stress * total,
        budget=budget,
        ref_price=ref_price,
        reasons=reasons,
        notes=notes,
    )
