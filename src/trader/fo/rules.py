"""파생 게이트(Phase 26, 스펙 §4) — 순수 함수. 거부 사유는 전부 모은다(첫 사유에서 멈추지 않음).

- 축소 다리(RESIZE↓·CLOSE·REPLACE 의 청산)는 `enabled`·수단·검증·대조·배수·입력 신선도와 무관하다.
  막는 것은 `paused`(기록만)뿐이고 off·dry_run 은 executor 가 본다.
- 진입 다리(OPEN·RESIZE↑)는 §4.2 거부 목록 → 대조 계수(§4.3) → 배수(§4.4, 기존 포지션은 상승
  금지) → 손절 단조(유리한 쪽만, HOLD 포함) → `sizing.size`(§4.5~4.7).
- `cross_factor` 표는 `test_cross_factor_table` 이 스펙 표와 1:1 로 고정한다.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from trader.config import KST, FoCfg
from trader.fo import sizing
from trader.fo.schemas import (
    ENTRY_DECISIONS,
    contract_side_of,
    kind_of,
    order_side,
)
from trader.fo.types import FoPositionRow, FoVerdict, GateInput, Leg
from trader.risk.ticks import quantize_tick, tick_table_for

ViewState = Literal["RISK_ON", "NEUTRAL", "RISK_OFF", "NONE"]
_ZERO = Decimal(0)
DELTA_MIN, DELTA_MAX = 0.05, 1.0


def _d(v) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


# --- 대조(§4.3) ---------------------------------------------------------------------------


def view_state_of(view: dict | None, *, today: date, min_confidence: float) -> ViewState:
    """오늘 시장 세션 제안 → 상태. 제안 없음·오늘 날짜 아님·확신 < 기준은 NONE."""
    if not view:
        return "NONE"
    d = str(view.get("date") or "")
    if d and d != today.isoformat():
        return "NONE"
    try:
        conf = float(view.get("confidence") or 0)
    except (TypeError, ValueError):
        return "NONE"
    if conf < min_confidence:
        return "NONE"
    stance = view.get("stance")
    return stance if stance in ("RISK_ON", "NEUTRAL", "RISK_OFF") else "NONE"


def is_against_regime(regime: str, direction: str) -> bool:
    """역행 = bull+SHORT, bear+LONG."""
    return (regime == "bull" and direction == "SHORT") or (regime == "bear" and direction == "LONG")


def cross_factor(
    regime: str,
    view_state: str,
    mode: str,
    direction: str,
    vehicle: str,
    *,
    sell_against_regime: bool = False,
) -> float:
    """스펙 §4.3 표. mode=off → 1.0(옵션 매도 역행 0 은 모드 무관)."""
    against = is_against_regime(regime, direction)
    if vehicle == "OPTION_SELL" and against and not sell_against_regime:
        return 0.0
    if mode == "off":
        return 1.0
    if direction == "FLAT":
        return 1.0
    if regime == "bull":
        if view_state == "RISK_OFF":
            return 0.5
        return (0.5 if mode == "tilt" else 0.0) if direction == "SHORT" else 1.0
    if regime == "bear":
        if view_state == "RISK_ON":
            return 0.5
        return (0.5 if mode == "tilt" else 0.0) if direction == "LONG" else 1.0
    # neutral
    if view_state == "RISK_ON":
        return 0.5 if direction == "SHORT" else 1.0
    if view_state == "RISK_OFF":
        return 0.5 if direction == "LONG" else 1.0
    return 1.0


# --- 단조(§4.4 R8) -------------------------------------------------------------------------


def monotone_stop(existing: Decimal | None, new: Decimal | None, direction: str) -> Decimal | None:
    """손절선은 유리한 쪽으로만: 롱은 올리기만(max), 숏은 내리기만(min)."""
    if new is None:
        return existing
    if existing is None:
        return new
    return max(existing, new) if direction == "LONG" else min(existing, new)


def _in_entry_window(now: datetime, cfg: FoCfg) -> bool:
    from trader.daemon import in_window

    local = now.astimezone(KST)
    return in_window(
        local.time().replace(second=0, microsecond=0),
        cfg.entry_window.start,
        cfg.entry_window.cutoff,
    )


def _quote_valid(gi: GateInput, reasons: list[str]) -> bool:
    q, cfg = gi.quote, gi.cfg
    ok = True
    if q is None:
        reasons.append("시세 없음")
        return False
    if q.price <= 0:
        reasons.append("시세 가격 0")
        ok = False
    if q.quote_asof is None or (gi.now - q.quote_asof) > timedelta(seconds=cfg.max_quote_age_s):
        reasons.append("시세 노후(quote_asof)")
        ok = False
    if gi.underlying is None or gi.underlying <= 0:
        reasons.append("기초지수 없음")
        ok = False
    if gi.decision.vehicle != "FUTURES":
        dl = q.delta
        if dl is None or not math.isfinite(dl) or not (DELTA_MIN <= abs(dl) <= DELTA_MAX):
            reasons.append(f"델타 무효({dl})")
            ok = False
        if q.iv is None or q.strike is None:
            reasons.append("옵션 IV·행사가 없음")
            ok = False
    if q.bid is None or q.ask is None or q.bid <= 0 or q.ask <= 0:
        reasons.append("호가 공백")
        ok = False
    return ok


# 진입 지정가가 체결 안 되는 쪽으로 현재가와 벌어질 수 있는 최대 비율(선물 2%·옵션 50%)
LIMIT_DEV_PCT = {"FUTURES": Decimal("0.02"), "OPTION": Decimal("0.5")}


def _limit_too_far(price: Decimal, q, *, side_of: str, kind: str) -> bool:
    """매수는 현재가보다 한참 아래, 매도는 한참 위의 지정가(체결 의사 없는 주문)를 거른다."""
    dev = LIMIT_DEV_PCT["FUTURES" if kind == "FUTURES" else "OPTION"]
    if q.price <= 0:
        return False
    gap = (q.price - price) if side_of == "BUY" else (price - q.price)
    return gap > q.price * dev


def _entry_price(
    gi: GateInput, contract_side: str, kind: str
) -> tuple[str, Decimal | None, Decimal]:
    """(주문 유형, 지정가, 기준가). LIMIT 은 tick 표로 맞춘 지정가가 기준가, MARKET 은 보수가
    (매수 ask+슬리피지·매도 bid−슬리피지). 옵션 매도 진입은 MARKET 금지 → 호가 1단 지정가."""
    d, cfg, q = gi.decision, gi.cfg, gi.quote
    assert q is not None and q.bid is not None and q.ask is not None
    table = tick_table_for(cfg.product_cfg(), kind)
    side = order_side(contract_side, entry=True)
    slip = _d(cfg.slippage_pt) if kind == "FUTURES" else _ZERO
    if d.order_type == "LIMIT" and d.entry_price is not None:
        px = quantize_tick(table, _d(d.entry_price))
        # 체결 가능한 쪽으로 넘친 지정가(매수 > ask·매도 < bid)는 호가 1단으로 보정 — 어차피 그
        # 가격에 체결되므로 기준가·계획 손실을 실제에 맞춘다(옵션 결정에 선물 가격을 적는 실수 포함)
        best = q.ask if side == "BUY" else q.bid
        if (side == "BUY" and px > best) or (side == "SELL" and px < best):
            px = quantize_tick(table, best)
        return "LIMIT", px, px
    if d.vehicle == "OPTION_SELL":
        px = quantize_tick(table, q.bid)
        return "LIMIT", px, px
    if d.order_type == "LIMIT":
        px = quantize_tick(table, q.ask if side == "BUY" else q.bid)
        return "LIMIT", px, px
    ref = (q.ask + slip) if side == "BUY" else (q.bid - slip)
    return "MARKET", None, max(ref, _ZERO)


def reduce_leg(
    cfg: FoCfg, quote, pos: FoPositionRow, intent: str, qty: int, *, market: bool = False
) -> Leg:
    """축소 다리 가격: 선물 시장가, 옵션은 호가 1단 지정가(호가 없거나 `market=True`(재시도 잔량)면
    시장가). 실행기(`fo/executor`)의 손절·만기·재시도 다리도 이 함수로 만든다."""
    q = quote
    side = order_side(pos.contract_side, entry=False)
    order_type, price = "MARKET", None
    if pos.kind != "FUTURES" and q is not None and not market:
        table = tick_table_for(cfg.product_cfg(), pos.kind)
        best = q.bid if side == "SELL" else q.ask
        if best is not None and best > 0:
            order_type, price = "LIMIT", quantize_tick(table, best)
    return Leg(
        intent=intent,
        order_side=side,
        qty=qty,
        order_type=order_type,
        price=price,
        planned_loss=_ZERO,
        code=pos.code,
        kind=pos.kind,
        position_id=pos.id,
    )


# --- 본체 -----------------------------------------------------------------------------------


def evaluate(gi: GateInput) -> FoVerdict:
    d, cfg, port, pos = gi.decision, gi.cfg, gi.port, gi.position
    reasons: list[str] = []
    notes: list[str] = []
    if gi.paused:
        reasons.append("일시정지(paused) — 기록만")

    # HOLD: 다리 없음, 손절 단조만
    if d.intent == "HOLD":
        stop = monotone_stop(pos.stop_pt if pos else None, _d(d.stop_pt) if d.stop_pt else None,
                             pos.direction if pos else d.direction)  # fmt: skip
        return FoVerdict(
            approved=not reasons, reasons=reasons, note="관망", stop_pt=stop,
            leverage_used=(pos.leverage_used or 0.0) if pos else 0.0,
            approved_mult=pos.approved_mult if pos else None,
            premium_stop=pos.premium_stop if pos else None,
            contract_side=pos.contract_side if pos else None, kind=pos.kind if pos else None,
            code=pos.code if pos else None,
        )  # fmt: skip

    # CLOSE·REPLACE: 축소 다리(전제는 확정 방향·잔량뿐)
    if d.intent in ("CLOSE", "REPLACE"):
        if pos is None:
            return FoVerdict(approved=False, reasons=[*reasons, "청산할 포지션 없음"])
        if pos.status == "unknown":
            return FoVerdict(approved=False, reasons=[*reasons, "unknown 포지션은 사람만 정리"])
        legs = [reduce_leg(cfg, gi.quote, pos, "CLOSE", pos.qty_held)] if pos.qty_held > 0 else []
        note = "청산" + (" 뒤 새 진입(REPLACE, 다음 틱 이후)" if d.intent == "REPLACE" else "")
        if not legs:
            note += " — 보유 0(살아 있는 진입 다리 회수만)"
        return FoVerdict(
            approved=not reasons, legs=legs, reasons=reasons, note=note,
            stop_pt=pos.stop_pt, approved_mult=pos.approved_mult, premium_stop=pos.premium_stop,
            contract_side=pos.contract_side, kind=pos.kind, code=pos.code, n_target=0,
        )  # fmt: skip

    # OPEN·RESIZE
    assert d.intent in ENTRY_DECISIONS
    contract_side = pos.contract_side if pos else contract_side_of(d.direction, d.vehicle)
    kind = pos.kind if pos else kind_of(d.direction, d.vehicle)
    code = pos.code if pos else (gi.contract.code if gi.contract else None)
    if pos is not None and (pos.direction != d.direction or pos.vehicle != d.vehicle):
        reasons.append(f"포지션 {pos.id} 의 방향·수단({pos.direction}/{pos.vehicle})과 결정이 다름")
    if d.intent == "OPEN" and pos is not None and pos.qty_held > 0:
        reasons.append("이미 보유 중 — RESIZE 로")
    if d.intent == "RESIZE" and pos is None:
        reasons.append("RESIZE 할 포지션 없음")

    # §4.2 진입 거부 목록
    if not cfg.enabled:
        reasons.append("fo.enabled 꺼짐(신규 금지)")
    if d.vehicle not in cfg.vehicles:
        reasons.append(f"허용 수단 아님: {d.vehicle}")
    if not port.verified.get(d.vehicle, False):
        reasons.append(f"수단 미검증: {d.vehicle}(fo verify / order-test)")
    if not gi.inputs_fresh:
        reasons.append("입력 노후(max_input_age_s)")
    if gi.historical:
        reasons.append("과거 asof — 신규 금지")
    if not _in_entry_window(gi.now, cfg):
        reasons.append("진입 창 밖")
    if gi.days_left is not None:
        roll = cfg.roll_days + (1 if d.vehicle == "OPTION_SELL" else 0)
        if gi.days_left <= roll:
            reasons.append(f"만기 roll 창 안(잔존 {gi.days_left}일)")
        if d.vehicle != "FUTURES" and gi.days_left < cfg.option.min_days_left:
            reasons.append(f"옵션 잔존일 {gi.days_left} < {cfg.option.min_days_left}")
    if port.halted:
        reasons.append("당일 손실 한도(halted)")
    if port.nav is not None and port.realized_today <= -(port.nav * _d(cfg.daily_loss_stop_pct)):
        reasons.append("당일 손실 한도 초과")
    if gi.killswitch:
        reasons.append("킬스위치")
    if port.cash is not None and port.cash.add_margin > 0:
        reasons.append(f"추가 증거금 {port.cash.add_margin}")
    if d.confidence < cfg.min_confidence:
        reasons.append(f"확신 {d.confidence:.2f} < {cfg.min_confidence}")
    if port.blocking:
        reasons.append(f"차단 상태: {port.blocking}")
    for p in port.positions:
        if pos is not None and p.id == pos.id:
            continue
        if p.status in ("closing", "exit_failed", "awaiting_settlement", "unknown"):
            reasons.append(f"청산 중·미정산 포지션 {p.id}({p.status})")
        elif p.direction != d.direction:
            reasons.append(f"반대 방향 보유 {p.id}({p.direction})")
    live_other = [x for x in port.legs if x.live and (pos is None or x.position_id != pos.id)]
    if pos is None and (len(port.positions) + 1 > cfg.max_positions or live_other):
        reasons.append(f"종목 수 한도 {cfg.max_positions}")
    if port.nav is None or port.nav <= 0:
        reasons.append("NAV 없음")
    if d.vehicle == "OPTION_SELL" and d.premium_stop_mult is None:
        reasons.append("옵션 매도에는 premium_stop_mult 필수")
    if d.vehicle in ("FUTURES", "OPTION_SELL") and gi.orderable is None:
        reasons.append("주문가능 조회 없음")
    regime_cap = cfg.regime_cap(gi.regime)
    if regime_cap <= 0:
        reasons.append(f"국면 신규 금지(leverage_by_regime[{gi.regime}] = 0)")
    cf = cross_factor(
        gi.regime, gi.view_state, cfg.cross_check, d.direction, d.vehicle,
        sell_against_regime=cfg.option.sell_against_regime,
    )  # fmt: skip
    if cf == 0.0:
        reasons.append(f"대조 차단(국면 {gi.regime} 역행 {d.direction}/{d.vehicle})")
    quote_ok = _quote_valid(gi, reasons)
    stop_new = monotone_stop(pos.stop_pt if pos else None, _d(d.stop_pt), d.direction)
    if pos is not None and stop_new != _d(d.stop_pt):
        notes.append(f"손절선 불리한 이동 거부 {d.stop_pt}→{stop_new}")
    if gi.underlying is not None and stop_new is not None:
        gap = _d(cfg.min_stop_pt)
        if d.direction == "LONG" and not (stop_new <= gi.underlying - gap):
            reasons.append(f"손절선 {stop_new} 이 기초지수 {gi.underlying} − {gap} 보다 높음(롱)")
        if d.direction == "SHORT" and not (stop_new >= gi.underlying + gap):
            reasons.append(f"손절선 {stop_new} 이 기초지수 {gi.underlying} + {gap} 보다 낮음(숏)")
        if d.take_profit_pt is not None:
            tp = _d(d.take_profit_pt)
            if (d.direction == "LONG" and tp <= gi.underlying) or (
                d.direction == "SHORT" and tp >= gi.underlying
            ):
                reasons.append(f"익절선 {tp} 부호 오류")

    # §4.4 배수(상승 금지)
    L = min(d.target_leverage, cfg.max_leverage, regime_cap)
    if L < d.target_leverage:
        notes.append(f"배수 {d.target_leverage}→{L}(한도)")
    if cf < 1.0 and cf > 0:
        L = L * cf
        notes.append(f"대조 계수 {cf} → 배수 {L:.2f}")
    if pos is not None and pos.leverage_used is not None and L > pos.leverage_used:
        notes.append(f"배수 상승 금지 {L:.2f}→{pos.leverage_used:.2f}")
        L = pos.leverage_used
    approved_mult: float | None = None
    if d.vehicle == "OPTION_SELL" and d.premium_stop_mult is not None:
        m = cfg.option.sell_stop_mult
        approved_mult = min(max(d.premium_stop_mult, m.min), m.max)
        if approved_mult != d.premium_stop_mult:
            notes.append(f"매도 보호 배수 {d.premium_stop_mult}→{approved_mult}(범위)")
        if pos is not None and pos.approved_mult is not None and approved_mult > pos.approved_mult:
            notes.append(f"보호 배수 상승 금지 {approved_mult}→{pos.approved_mult}")
            approved_mult = pos.approved_mult
    elif pos is not None:
        approved_mult = pos.approved_mult

    base = FoVerdict(
        approved=False, reasons=reasons, cross_factor=cf, stop_pt=stop_new,
        approved_mult=approved_mult, contract_side=contract_side, kind=kind, code=code,
        leverage_used=L, premium_stop=pos.premium_stop if pos else None,
    )  # fmt: skip
    if reasons or not quote_ok or code is None:
        if code is None:
            base.reasons.append("계약 코드 없음(마스터·체인)")
        base.note = "; ".join(notes)
        return base

    # §4.5 사이징
    order_type, price, ref = _entry_price(gi, contract_side, kind)
    assert gi.quote is not None
    if price is not None and _limit_too_far(
        price, gi.quote, side_of=order_side(contract_side, entry=True), kind=kind
    ):
        base.reasons.append(f"지정가 괴리: {price} vs 현재가 {gi.quote.price}")
        base.note = "; ".join(notes)
        return base
    premium_stop = pos.premium_stop if pos else None
    if d.vehicle != "FUTURES":
        cand = sizing.premium_stop_candidate(
            d.vehicle, ref, approved_mult=approved_mult, buy_stop_pct=cfg.option.buy_stop_pct
        )
        premium_stop = sizing.premium_stop_merge(premium_stop, cand, d.vehicle)
    sr = sizing.size(
        gi, L=L, approved_mult=approved_mult, premium_stop=premium_stop,
        stop_pt=stop_new if stop_new is not None else _d(d.stop_pt), ref_price=ref,
    )  # fmt: skip
    notes.extend(sr.notes)
    base.reasons.extend(sr.reasons)
    base.premium_stop = premium_stop
    base.n_target = sr.n_target
    base.exposure_after = sr.exposure_after
    base.stress_loss_after = sr.stress_loss_after
    base.lifetime_budget = sr.budget
    base.leverage_used = float(sr.exposure_after / port.nav) if port.nav else L  # type: ignore[arg-type]
    held = pos.qty_held if pos else 0
    legs: list[Leg] = []
    if sr.n_leg > 0 and not base.reasons:
        legs.append(
            Leg(
                intent="OPEN" if d.intent == "OPEN" else "RESIZE_UP",
                order_side=order_side(contract_side, entry=True),
                qty=sr.n_leg,
                order_type=order_type,
                price=price,
                planned_loss=sr.planned_loss_leg,
                code=code,
                kind=kind,
                position_id=pos.id if pos else None,
            )
        )
    elif d.intent == "RESIZE" and pos is not None and sr.n_target < held:
        legs.append(reduce_leg(cfg, gi.quote, pos, "RESIZE_DOWN", held - sr.n_target))
        notes.append(f"목표 {sr.n_target} < 보유 {held} → 감액 {held - sr.n_target}")
    elif d.intent == "OPEN" and sr.n_leg <= 0:
        base.reasons.append("수량 0")
    elif d.intent == "RESIZE" and sr.n_leg <= 0 and not base.reasons:
        notes.append("목표와 보유·미체결이 같음 — 다리 없음")
    base.legs = legs
    base.approved = not base.reasons
    base.note = "; ".join(notes)
    return base
