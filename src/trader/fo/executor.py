"""파생 레인 실행기(Phase 26 단계 2 Task 2.6, 스펙 §5). 결정 봉투 하나를 계좌 하나에서 다리
단위로 실행한다.

- **진입 다리**는 실행 직전에 시세·잔고·주문가능을 **다시 받아** `rules.evaluate` 를 처음부터
  다시 계산한 결과로만 낸다(아침 결정의 수량은 참고값일 뿐 재사용하지 않는다). 그 뒤
  `store.reserve`(계좌 총량 재검사 + 다리 `prepared`) → `sending` 커밋 → `place_order` →
  `accepted|rejected|unknown` → `orders` 행(asset='fo') → 체크포인트·알림 순서다.
- **축소 다리**(RESIZE↓·CLOSE·손절·만기·마감·축소)의 전제는 확정 방향·확정 잔량·활성 축소 다리
  하나뿐이다 — 체인·IV·지수·시세 노후는 막지 않는다(가격은 호가 1단, 없으면 시장가).
- `sending`·`unknown` 다리는 절대 다시 보내지 않는다. 확정은 `positions.recover`/`_confirm_leg`
  (체결 조회)가 한다. 접수 뒤 원장 기록이 실패하면 `OrderBookkeepingError`(다리는 `sending` 으로
  남아 복구 틱이 확정).
- `paused`·`off`·`dry_run`·실전 계좌는 **기록만**(취소 요청도 내지 않는다).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from trader.broker import fo_master
from trader.broker.errors import UNKNOWN_RESPONSE_CODE, KisApiError
from trader.broker.fo import CONTRACT_VERSION
from trader.broker.fo_master import Contract
from trader.broker.models import FoBalance, FoOrderable, FoOrderRequest, FoQuote
from trader.config import KST, AccountConfig, Settings
from trader.control import control
from trader.fo import positions, rules, sizing, store
from trader.fo.schemas import FoDecisionEnvelope, contract_side_of, kind_of, order_side
from trader.fo.types import (
    FoPortfolio,
    FoPositionRow,
    FoVerdict,
    GateInput,
    Leg,
    is_entry,
    is_reduce,
)
from trader.ledger import repo
from trader.ledger.db import now_iso
from trader.notify import messages
from trader.notify.telegram import Notifier, NullNotifier
from trader.orchestrator.executor import OrderBookkeepingError, effective_mode
from trader.orchestrator.run_store import checkpoint_payload, market_dir, write_checkpoint
from trader.risk import killswitch
from trader.risk.ticks import quantize_tick, tick_table_for

log = logging.getLogger("trader.fo.executor")

VEHICLES = ("FUTURES", "OPTION_BUY", "OPTION_SELL")
CLOSING_INTENTS = ("CLOSE", "STOP", "EXPIRY", "SESSION_CLOSE", "RISK_REDUCE")


def verified_key(settings: Settings, account: AccountConfig, vehicle: str) -> str:
    """control `fo_verified:<env>:<계좌>:<상품>:<수단>` 키(`fo verify` 가 쓰고 게이트가 읽는다)."""
    return f"fo_verified:{account.env}:{account.id}:{settings.fo.product}:{vehicle}"


def halted_key(account_id: str) -> str:
    """당일 손실 한도로 신규를 막는 날짜 플래그(값 = 운영일 ISO)."""
    return f"fo_halted:{account_id}"


@dataclass
class FoExecContext:
    settings: Settings
    accounts: list[AccountConfig]  # asset == "fo" 만
    conn: sqlite3.Connection
    brokers: dict[str, Any] = field(default_factory=dict)  # account_id → KisFoBroker | FakeFoBroker
    market_client: Any | None = None  # KisMarket | FakeMarket (get_fo_price·get_kospi200)
    master: list[Contract] = field(default_factory=list)
    notifier: Notifier = field(default_factory=NullNotifier)
    state: Path | None = None
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    dry_run: bool = False
    historical: bool = False  # 과거 asof 세션 — 신규 금지(판정은 기록)
    regime_of: Callable[[], str] = field(default=lambda: "neutral")
    view_of: Callable[[], dict | None] = field(default=lambda: None)


@dataclass
class FreshInputs:
    """실행 직전 재조회 묶음. 실패한 절은 None + `errors`(축소는 이것 없이도 간다)."""

    quote: FoQuote | None = None
    underlying: Decimal | None = None
    index_asof: datetime | None = None
    balance: FoBalance | None = None
    orderable_fn: Callable[[str, str, Decimal | None, str], FoOrderable | None] = field(
        default=lambda *_: None
    )
    errors: list[str] = field(default_factory=list)


@dataclass
class RecallResult:
    cancelled: int = 0
    confirmed_filled: int = 0
    pending: int = 0  # 취소 미확정·주문번호 없는 다리(복구 틱이 확정)
    errors: list[str] = field(default_factory=list)


# --- 입력 ------------------------------------------------------------------------------


def _no_quote(_: Contract) -> date | None:
    return None


def quote_of(
    ctx: FoExecContext, code: str, kind: str, fi: FreshInputs | None = None
) -> FoQuote | None:
    """시세 1건(실패는 None + fi.errors). 지수 절이 비어 있으면 시세의 기초지수로 채운다."""
    mc = ctx.market_client
    if mc is None:
        if fi is not None:
            fi.errors.append("market_client 없음")
        return None
    try:
        q = mc.get_fo_price(code, kind)
    except Exception as e:  # noqa: BLE001 — 시세 실패는 판정에서 '시세 없음' 으로 거부
        if fi is not None:
            fi.errors.append(f"quote: {type(e).__name__}: {e}")
        return None
    if fi is not None:
        fi.quote = q
        if fi.underlying is None and q.underlying is not None:
            fi.underlying, fi.index_asof = q.underlying, q.underlying_asof
    return q


def fetch_fresh(
    ctx: FoExecContext, account: AccountConfig, code: str | None, kind: str | None
) -> FreshInputs:
    """실행 직전 재조회: 코스피200·(코드가 있으면) 시세·잔고. 주문가능은 지연 호출(코드·방향·가격이
    사이징 뒤에 정해진다). 주문가능 응답에 현금이 없으면 잔고의 주문가능현금을 쓴다."""
    fi = FreshInputs()
    mc = ctx.market_client
    if mc is not None:
        try:
            fi.underlying, fi.index_asof = mc.get_kospi200()
        except Exception as e:  # noqa: BLE001
            fi.errors.append(f"kospi200: {type(e).__name__}: {e}")
    else:
        fi.errors.append("market_client 없음")
    if code is not None and kind is not None:
        quote_of(ctx, code, kind, fi)
    broker = ctx.brokers.get(account.id)
    if broker is None:
        fi.errors.append("broker 없음")
        return fi
    try:
        fi.balance = broker.get_balance()
    except Exception as e:  # noqa: BLE001
        fi.errors.append(f"balance: {type(e).__name__}: {e}")

    def orderable_fn(c: str, side: str, price: Decimal | None, ot: str) -> FoOrderable | None:
        try:
            o = broker.orderable(c, side, price, ot)
        except Exception as e:  # noqa: BLE001
            fi.errors.append(f"orderable: {type(e).__name__}: {e}")
            return None
        if o.cash is None and fi.balance is not None:
            o = o.model_copy(update={"cash": fi.balance.cash.orderable_cash})
        return o

    fi.orderable_fn = orderable_fn
    return fi


def build_portfolio(
    ctx: FoExecContext, account: AccountConfig, balance: FoBalance | None, today: date
) -> FoPortfolio:
    conn, s = ctx.conn, ctx.settings
    unverified = [u.code for u in balance.unverified] if balance is not None else None
    blocking = positions.has_blocking_state(conn, account.id, unverified=unverified)
    if balance is None:
        blocking = blocking or "잔고 조회 실패"
    verified = {
        v: control.get_flag(conn, verified_key(s, account, v)) == CONTRACT_VERSION for v in VEHICLES
    }
    halted = control.get_flag(conn, halted_key(account.id)) == today.isoformat()
    return FoPortfolio(
        account_id=account.id,
        nav=balance.cash.nav if balance is not None else None,
        cash=balance.cash if balance is not None else None,
        positions=store.open_positions(conn, account.id),
        legs=store.live_legs(conn, account_id=account.id),
        blocking=blocking,
        halted=halted,
        verified=verified,
        realized_today=balance.cash.realized_today if balance is not None else Decimal(0),
    )


def _inputs_fresh(fi: FreshInputs, now: datetime, max_age_s: int) -> bool:
    if fi.quote is None or fi.balance is None:
        return False
    stamps = [fi.quote.quote_asof or fi.quote.received_at, fi.balance.cash.received_at]
    return all((now - t).total_seconds() <= max_age_s for t in stamps)


def _days_left(quote: FoQuote | None, contract: Contract | None, today: date) -> int | None:
    if quote is not None and quote.days_left is not None:
        return quote.days_left
    if quote is not None and quote.last_trade_date is not None:
        return (quote.last_trade_date - today).days
    if contract is not None and contract.expiry_ym:
        return (fo_master.second_thursday(contract.expiry_ym) - today).days
    return None


def _resolve_contract(
    ctx: FoExecContext, env: FoDecisionEnvelope, pos: FoPositionRow | None, *,
    underlying: Decimal | None, today: date,
) -> Contract | None:  # fmt: skip
    d, cfg = env.decision, ctx.settings.fo
    pcfg = cfg.product_cfg()
    if pos is not None:
        return next((c for c in ctx.master if c.code == pos.code), None)
    if d.vehicle == "FUTURES":
        return fo_master.front_month(
            ctx.master, pcfg, "FUTURES", today=today, roll_days=cfg.roll_days,
            last_trade_date_of=_no_quote,
        )  # fmt: skip
    if underlying is None:
        return None
    return fo_master.resolve_option(
        ctx.master, pcfg, call_put=kind_of(d.direction, d.vehicle), strike_rule=d.strike_rule,
        expiry_rule=d.expiry_rule, underlying=underlying, today=today, roll_days=cfg.roll_days,
        last_trade_date_of=_no_quote,
    )  # fmt: skip


def _name_of(ctx: FoExecContext, code: str) -> str | None:
    return next((c.name for c in ctx.master if c.code == code), None)


# --- 기록·알림 -----------------------------------------------------------------------


def _ckpt(ctx: FoExecContext, today: date, name: str, data: dict) -> None:
    """체크포인트 기록(Decimal·날짜는 문자열). 실패는 경고만 — 주문 흐름을 끊지 않는다."""
    try:
        safe = json.loads(json.dumps(data, default=str, ensure_ascii=False))
        write_checkpoint(
            market_dir(today, "KR", ctx.state, create=True), name, checkpoint_payload(safe)
        )
    except Exception as e:  # noqa: BLE001 — 체크포인트 실패는 경고(주문 흐름을 끊지 않음)
        log.warning("파생 체크포인트 %s 기록 실패: %s", name, e)


def _notify(ctx: FoExecContext, text: str) -> None:
    try:
        ctx.notifier.send(text)
    except Exception as e:  # noqa: BLE001
        log.warning("알림 전송 실패: %s", type(e).__name__)


def _record_only(ctx: FoExecContext, mode: str, paused: bool) -> str | None:
    """주문·취소를 내지 않는 사유(없으면 None)."""
    if ctx.dry_run:
        return "dry_run"
    if paused:
        return "paused"
    if mode != "auto":
        return f"mode={mode}"
    return None


def _today(ctx: FoExecContext) -> tuple[datetime, date]:
    now = ctx.now()
    return now, now.astimezone(KST).date()


def _stage(prefix: str, account_id: str, round: int) -> str:
    return f"{prefix}_{account_id}" + (f"_r{round}" if round else "")


# --- 전송 ------------------------------------------------------------------------------


def _transmit(
    ctx: FoExecContext,
    account: AccountConfig,
    *,
    leg_id: int,
    position_id: int,
    req: FoOrderRequest,
    ref_price: Decimal,
    mode: str,
    stage: str,
    today: date,
    intent: str,
    extra: dict[str, Any],
) -> dict[str, Any]:
    """`prepared → sending` 선점 커밋(`store.claim_leg` — 회수가 먼저 cancelled 로 바꿨으면
    보내지 않는다) → 전송 → 다리·orders 행·체크포인트·알림. 응답 없음은 unknown 으로 남긴다
    (주문이 살아 있을 수 있다)."""
    conn = ctx.conn
    multiplier = ctx.settings.fo.product_cfg().multiplier
    sent_at = ctx.now().isoformat()
    with store.tx(conn):
        claimed = store.claim_leg(conn, leg_id, sent_at=sent_at)
    if not claimed:
        cur = store.get_leg(conn, leg_id)
        result = {
            "leg_id": leg_id, "position_id": position_id, "intent": intent, "status": "recalled",
            "message": f"다리 {leg_id} 가 전송 전 {cur.status if cur else '없음'} — 보내지 않음",
            "mode": mode, **extra,
        }  # fmt: skip
        _ckpt(ctx, today, stage, result)
        return result
    broker = ctx.brokers[account.id]
    status, order_no, msg = "failed", None, ""
    try:
        res = broker.place_order(req)
        status = "accepted" if res.accepted else "rejected"
        order_no, msg = res.order_no, res.message
    except KisApiError as e:
        status = "unknown" if e.msg_cd == UNKNOWN_RESPONSE_CODE else "rejected"
        msg = str(e)
    except ValueError as e:
        status, msg = "rejected", str(e)
    leg_status = {"accepted": "accepted", "unknown": "unknown"}.get(status, "rejected")
    order_id: int | None = None
    if status in ("accepted", "unknown"):
        try:
            order_id = repo.insert_order(
                conn,
                decision_id=None,
                account_id=account.id,
                ticker=req.code,
                market="KR",
                side=req.side,
                qty=req.qty,
                order_type=req.order_type,
                price=req.price,
                status=status,
                order_no=order_no,
                message=msg,
                mode=mode,
                amount=Decimal(req.qty) * ref_price * multiplier,
                asset="fo",
            )
        except Exception as e:  # noqa: BLE001
            log.critical("파생 주문 기록 실패(접수번호 %s): %s", order_no, e)
            raise OrderBookkeepingError(order_no, e) from e
    with store.tx(conn):
        store.set_leg(
            conn,
            leg_id,
            status=leg_status,
            order_id=order_id,
            order_no=order_no,
            accepted_at=ctx.now().isoformat() if status == "accepted" else None,
        )
        if leg_status == "rejected" and is_entry(intent):
            store.release_if_empty(conn, position_id, f"진입 거부: {msg}")
    result = {
        "leg_id": leg_id,
        "position_id": position_id,
        "intent": intent,
        "request": req.model_dump(mode="json"),
        "status": status,
        "order_no": order_no,
        "message": msg,
        "mode": mode,
        "order_id": order_id,
        **extra,
    }
    _ckpt(ctx, today, stage, result)
    name = _name_of(ctx, req.code)
    if status == "unknown":
        _notify(
            ctx,
            messages.fo_unknown_order(
                account=account.id, code=req.code, name=name, side=req.side, qty=req.qty, err=msg
            ),
        )
    else:
        _notify(
            ctx,
            messages.fo_order(
                account=account.id,
                code=req.code,
                name=name,
                intent=intent,
                side=req.side,
                qty=req.qty,
                order_type=req.order_type,
                price=req.price,
                status=status,
                order_no=order_no,
                leverage=extra.get("leverage_used"),
                stop_pt=extra.get("stop_pt"),
            ),  # fmt: skip
        )
    return result


def _send_entry(
    ctx: FoExecContext,
    account: AccountConfig,
    *,
    leg: Leg,
    verdict: FoVerdict,
    env: FoDecisionEnvelope,
    pos: FoPositionRow | None,
    port: FoPortfolio,
    quote: FoQuote | None,
    round: int,
    stage: str,
    mode: str,
    today: date,
) -> dict[str, Any]:
    d, cfg = env.decision, ctx.settings.fo
    per_contract = (leg.planned_loss / leg.qty) if leg.qty else Decimal(0)
    stop_fut = None
    if leg.kind == "FUTURES" and verdict.stop_pt is not None and quote is not None:
        stop_fut = sizing.stop_fut_of(
            verdict.stop_pt, quote.basis or Decimal(0), d.direction, Decimal(str(cfg.slippage_pt))
        )
    nav_at_open = pos.nav_at_open if pos is not None else port.nav
    budget_limit = (
        nav_at_open * Decimal(str(cfg.risk_per_trade_pct)) if nav_at_open is not None else None
    )
    if pos is None:
        ltd = quote.last_trade_date.isoformat() if quote and quote.last_trade_date else None
        new_position = dict(
            account_id=account.id, code=leg.code, kind=leg.kind,
            contract_side=verdict.contract_side, direction=d.direction, vehicle=d.vehicle,
            status="entering", qty_target=verdict.n_target, qty_held=0,
            leverage_used=verdict.leverage_used, stop_pt=verdict.stop_pt, stop_fut=stop_fut,
            tp_pt=d.take_profit_pt, approved_mult=verdict.approved_mult,
            premium_stop=verdict.premium_stop,
            entry_premium=leg.price if leg.kind != "FUTURES" else None, nav_at_open=nav_at_open,
            horizon_until=(today + timedelta(days=d.horizon_days)).isoformat(),
            last_trade_date=ltd, decision_date=today.isoformat(), round=round, opened_at=now_iso(),
        )  # fmt: skip
        changes: dict[str, Any] = {}
        new_status = None
    else:
        new_position = None
        changes = dict(
            qty_target=verdict.n_target, stop_pt=verdict.stop_pt, stop_fut=stop_fut,
            leverage_used=verdict.leverage_used, approved_mult=verdict.approved_mult,
            premium_stop=verdict.premium_stop,
        )  # fmt: skip
        new_status = "resizing" if pos.status != "resizing" else None
    plan = store.ReservePlan(
        intent=leg.intent, round=round, qty=leg.qty, price=leg.price, order_type=leg.order_type,
        position_id=pos.id if pos else None, expected_version=pos.version if pos else None,
        new_position=new_position, new_status=new_status, planned_loss_per_contract=per_contract,
        planned_loss_existing=verdict.planned_loss_existing, budget_limit=budget_limit,
        position_changes=changes,
    )  # fmt: skip
    with store.tx(ctx.conn):
        rr = store.reserve(
            ctx.conn,
            account.id,
            plan,
            max_positions=cfg.max_positions,
            max_contracts=cfg.max_contracts,
        )
    if not rr.ok:
        result = {"intent": leg.intent, "status": "reserve_rejected", "reasons": rr.reasons}
        _ckpt(ctx, today, stage, result)
        _notify(
            ctx,
            messages.fo_reject(
                account=account.id,
                code=leg.code,
                name=_name_of(ctx, leg.code),
                intent=leg.intent,
                reasons=rr.reasons,
            ),  # fmt: skip
        )
        return result
    assert rr.leg_id is not None and rr.position_id is not None
    req = FoOrderRequest(
        code=leg.code, side=leg.order_side, qty=leg.qty, order_type=leg.order_type, price=leg.price
    )
    ref = leg.price if leg.price is not None else (quote.price if quote else Decimal(0))
    return _transmit(
        ctx, account, leg_id=rr.leg_id, position_id=rr.position_id, req=req, ref_price=ref,
        mode=mode, stage=stage, today=today, intent=leg.intent,
        extra={"leverage_used": verdict.leverage_used, "stop_pt": verdict.stop_pt,
               "n_target": verdict.n_target, "request_id": env.request_id},
    )  # fmt: skip


def _send_reduce(
    ctx: FoExecContext,
    account: AccountConfig,
    pos: FoPositionRow,
    leg: Leg,
    *,
    round: int,
    stage: str,
    mode: str,
    today: date,
    reason: str,
    changes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """축소 다리 예약·전송. `changes` 는 결정이 함께 바꾸는 포지션 열(목표 수량·손절선 등).
    `exit_failed` 는 그대로 둔다(재시도 중임을 보이기 위해)."""
    target = "resizing" if leg.intent == "RESIZE_DOWN" else "closing"
    plan = store.ReservePlan(
        intent=leg.intent, round=round, qty=leg.qty, price=leg.price, order_type=leg.order_type,
        position_id=pos.id, expected_version=pos.version,
        new_status=None if pos.status in (target, "exit_failed") else target,
        position_changes=changes or {},
    )  # fmt: skip
    with store.tx(ctx.conn):
        rr = store.reserve(
            ctx.conn, account.id, plan, max_positions=ctx.settings.fo.max_positions,
            max_contracts=ctx.settings.fo.max_contracts,
        )  # fmt: skip
    if not rr.ok:
        result = {"intent": leg.intent, "status": "reserve_rejected", "reasons": rr.reasons}
        _ckpt(ctx, today, stage, result)
        return result
    assert rr.leg_id is not None
    req = FoOrderRequest(
        code=leg.code, side=leg.order_side, qty=leg.qty, order_type=leg.order_type, price=leg.price
    )
    quote = quote_of(ctx, pos.code, pos.kind)
    ref = leg.price if leg.price is not None else (quote.price if quote else Decimal(0))
    return _transmit(
        ctx, account, leg_id=rr.leg_id, position_id=pos.id, req=req, ref_price=ref, mode=mode,
        stage=stage, today=today, intent=leg.intent, extra={"reason": reason},
    )  # fmt: skip


# --- 결정 실행 ------------------------------------------------------------------------


def run_leg(
    ctx: FoExecContext,
    account: AccountConfig,
    env: FoDecisionEnvelope,
    *,
    round: int = 0,
    source: str = "decision",
    _recalled: bool = False,
) -> list[dict[str, Any]]:
    """결정 봉투를 이 계좌에서 실행한다. 반환은 다리별 결과(기록만 한 경우도 1건). `_recalled` 는
    미체결 진입 회수 뒤의 재계산 호출(한 번만)."""
    d, cfg, conn = env.decision, ctx.settings.fo, ctx.conn
    now, today = _today(ctx)
    rc_stage = _stage("_fo_risk_check", account.id, round)
    od_stage = _stage("_fo_order", account.id, round)
    if account.asset != "fo" or account.env != "paper":
        why = (
            "파생 계좌가 아님"
            if account.asset != "fo"
            else "실전 파생 계좌는 Phase 26 범위 밖(skip)"
        )
        _ckpt(ctx, today, rc_stage, {"skipped": why, "request_id": env.request_id})
        return [{"skipped": why}]
    mode, demoted = effective_mode(conn, account)
    paused = control.is_paused(conn)
    hold = _record_only(ctx, mode, paused)

    pos: FoPositionRow | None = None
    if env.position_id is not None:
        pos = store.get_position(conn, env.position_id)
        if pos is None or pos.account_id != account.id:
            return _reject(ctx, today, rc_stage, env, [f"포지션 {env.position_id} 없음"])
        if env.expected_version is not None and pos.version != env.expected_version:
            return _reject(
                ctx, today, rc_stage, env,
                [f"포지션 버전 불일치 {pos.version} ≠ 기대 {env.expected_version}"],
            )  # fmt: skip
    else:
        opened = store.open_positions(conn, account.id)
        pos = opened[0] if opened else None

    # 축소 결정
    if d.intent in ("CLOSE", "REPLACE"):
        if pos is None:
            return _reject(ctx, today, rc_stage, env, ["청산할 포지션 없음"])
        _ckpt(
            ctx,
            today,
            rc_stage,
            {
                "intent": d.intent,
                "position_id": pos.id,
                "mode": mode,
                "record_only": hold,
                "request_id": env.request_id,
            },
        )
        return [reduce(ctx, account, pos, intent="CLOSE", qty=None, reason=source, round=round)]

    # 진입·HOLD: 재조회 → 전체 재계산
    fi = fetch_fresh(ctx, account, pos.code if pos else None, pos.kind if pos else None)
    contract = _resolve_contract(ctx, env, pos, underlying=fi.underlying, today=today)
    if pos is None and contract is not None and fi.quote is None:
        quote_of(ctx, contract.code, contract.kind, fi)
    port = build_portfolio(ctx, account, fi.balance, today)
    code = pos.code if pos else (contract.code if contract else None)
    orderable: FoOrderable | None = None
    if d.intent in ("OPEN", "RESIZE") and code is not None:
        cs = pos.contract_side if pos else contract_side_of(d.direction, d.vehicle)
        px = (
            Decimal(str(d.entry_price))
            if d.entry_price is not None
            else (fi.quote.price if fi.quote else None)
        )
        if px is not None:
            # 호가단위 밖 가격은 브로커 `_price_str` 이 거부해 '주문가능 조회 없음' 이 되므로 맞춘다
            kind = pos.kind if pos else kind_of(d.direction, d.vehicle)
            px = quantize_tick(tick_table_for(cfg.product_cfg(), kind), px)
        orderable = fi.orderable_fn(code, order_side(cs, entry=True), px, d.order_type)
    gi = GateInput(
        decision=d, env=env, port=port, cfg=cfg, quote=fi.quote, underlying=fi.underlying,
        index_asof=fi.index_asof, regime=ctx.regime_of(),
        view_state=rules.view_state_of(
            ctx.view_of(), today=today, min_confidence=ctx.settings.hedge.agent_min_confidence
        ),
        now=now, today=today, killswitch=killswitch.reason(conn, account.id) is not None,
        paused=paused, orderable=orderable,
        inputs_fresh=_inputs_fresh(fi, now, cfg.max_input_age_s),
        historical=ctx.historical, contract=contract,
        days_left=_days_left(fi.quote, contract, today), position=pos,
    )  # fmt: skip
    v = rules.evaluate(gi)
    if fi.errors:
        v.note = (v.note + "; " if v.note else "") + "입력 오류: " + " | ".join(fi.errors)
    rc = {
        "verdict": v.as_dict(), "mode": mode, "demoted": demoted, "record_only": hold,
        "source": source, "request_id": env.request_id, "fresh_errors": fi.errors,
        "position_id": pos.id if pos else None, "contract": contract.code if contract else None,
    }  # fmt: skip
    _ckpt(ctx, today, rc_stage, rc)

    if v.approved and v.recall_entries and pos is not None:
        # 목표 < 보유 + 미체결 진입(G2-5): 미체결 진입을 회수(취소 확정)한 뒤 한 번 다시 계산한다
        if hold is not None:
            r = {
                "stage": rc_stage, "approved": True, "legs": [], "note": v.note,
                "status": "recorded", "record_only": hold, "request_id": env.request_id,
            }  # fmt: skip
            _ckpt(ctx, today, od_stage, r)
            return [r]
        if _recalled:
            return [{"stage": rc_stage, "approved": True, "legs": [], "status": "blocked",
                     "note": v.note + "; 회수 뒤에도 미체결 진입 남음"}]  # fmt: skip
        rec = recall_entries(ctx, account, pos.id, reason=f"{source}: 목표 축소")
        if rec.pending:
            return [{
                "stage": rc_stage, "status": "blocked",
                "note": f"미확정 진입 다리 {rec.pending}건", "recall": rec.__dict__,
            }]  # fmt: skip
        cur = store.get_position(conn, pos.id)
        if cur is None or cur.status in ("closed", "settled"):
            return [{"stage": rc_stage, "status": cur.status if cur else "missing"}]
        env2 = (
            env if env.expected_version is None
            else env.model_copy(update={"expected_version": cur.version})
        )
        return run_leg(ctx, account, env2, round=round, source=source, _recalled=True)

    if d.intent == "HOLD":
        if pos is not None and hold is None and v.stop_pt is not None and v.stop_pt != pos.stop_pt:
            changes = {"stop_pt": v.stop_pt, **_stop_fut_change(cfg, pos, fi.quote, v.stop_pt)}
            with store.tx(conn):
                store.transition(conn, pos.id, expected_version=pos.version, **changes)
        return [{"stage": rc_stage, "approved": v.approved, "legs": [], "note": v.note}]
    if not v.approved or not v.legs:
        if hold is not None:
            r = {
                "stage": rc_stage, "approved": v.approved, "reasons": v.reasons, "note": v.note,
                "status": "recorded", "record_only": hold, "request_id": env.request_id,
            }  # fmt: skip
            _ckpt(ctx, today, od_stage, r)
            return [r]
        if not v.approved and code is not None:
            _notify(
                ctx,
                messages.fo_reject(
                    account=account.id,
                    code=code,
                    name=_name_of(ctx, code),
                    intent=d.intent,
                    reasons=v.reasons,
                ),  # fmt: skip
            )
        return [{"stage": rc_stage, "approved": v.approved, "reasons": v.reasons, "note": v.note}]

    results: list[dict[str, Any]] = []
    for leg in v.legs:
        if hold is not None:
            r = {
                "intent": leg.intent, "status": "recorded", "record_only": hold,
                "leg": _leg_json(leg), "request_id": env.request_id,
            }  # fmt: skip
            _ckpt(ctx, today, od_stage, r)
            if hold == "dry_run":
                _notify(
                    ctx,
                    messages.fo_order(
                        account=account.id,
                        code=leg.code,
                        name=_name_of(ctx, leg.code),
                        intent=leg.intent,
                        side=leg.order_side,
                        qty=leg.qty,
                        order_type=leg.order_type,
                        price=leg.price,
                        status="dry_run",
                        order_no=None,
                        leverage=v.leverage_used,
                        stop_pt=v.stop_pt,
                        dry_run=True,
                    ),  # fmt: skip
                )
            results.append(r)
            continue
        if is_entry(leg.intent):
            results.append(
                _send_entry(
                    ctx,
                    account,
                    leg=leg,
                    verdict=v,
                    env=env,
                    pos=pos,
                    port=port,
                    quote=fi.quote,
                    round=round,
                    stage=od_stage,
                    mode=mode,
                    today=today,
                )  # fmt: skip
            )
        else:
            # 결정의 RESIZE_DOWN 도 공통 축소 경로(진입 회수 → 잔고 확정 → 보유 − 목표). 새 목표·
            # 손절선 등은 먼저 저장하고(`reduce` 가 qty_target 으로 잔량을 센다) 축소는 `reduce` 가
            # 낸다
            assert pos is not None
            changes = dict(
                qty_target=v.n_target, stop_pt=v.stop_pt, leverage_used=v.leverage_used,
                approved_mult=v.approved_mult, premium_stop=v.premium_stop,
                **_stop_fut_change(cfg, pos, fi.quote, v.stop_pt),
            )  # fmt: skip
            with store.tx(conn):
                store.transition(conn, pos.id, expected_version=pos.version, **changes)
            cur = store.get_position(conn, pos.id)
            assert cur is not None
            results.append(
                reduce(
                    ctx, account, cur, intent="RESIZE_DOWN", qty=None, reason=source, round=round,
                    stage=od_stage,
                )  # fmt: skip
            )
    return results


def _stop_fut_change(
    cfg: Any, pos: FoPositionRow, quote: FoQuote | None, stop_pt: Decimal | None
) -> dict[str, Any]:
    """손절선이 바뀔 때 선물 환산 보호선(`stop_fut`)도 함께, 유리한 쪽으로만(단계 2 리뷰 S2-10).
    시세(베이시스)가 없으면 기존 `stop_fut − stop_pt` 오프셋을 유지해 갱신한다."""
    if pos.kind != "FUTURES" or stop_pt is None:
        return {}
    slip = Decimal(str(cfg.slippage_pt))
    if quote is not None and quote.basis is not None:
        new = sizing.stop_fut_of(stop_pt, quote.basis, pos.direction, slip)
    elif pos.stop_fut is not None and pos.stop_pt is not None:
        new = stop_pt + (pos.stop_fut - pos.stop_pt)
    else:
        new = sizing.stop_fut_of(stop_pt, None, pos.direction, slip)
    return {"stop_fut": rules.monotone_stop(pos.stop_fut, new, pos.direction)}


def _leg_json(leg: Leg) -> dict[str, Any]:
    return {
        "intent": leg.intent, "order_side": leg.order_side, "qty": leg.qty,
        "order_type": leg.order_type, "price": str(leg.price) if leg.price is not None else None,
        "code": leg.code, "kind": leg.kind, "planned_loss": str(leg.planned_loss),
    }  # fmt: skip


def _reject(
    ctx: FoExecContext, today: date, stage: str, env: FoDecisionEnvelope, reasons: list[str]
) -> list[dict[str, Any]]:
    _ckpt(ctx, today, stage, {"approved": False, "reasons": reasons, "request_id": env.request_id})
    return [{"stage": stage, "approved": False, "reasons": reasons}]


# --- 진입 회수·축소·재시도 -----------------------------------------------------------------


def recall_entries(
    ctx: FoExecContext, account: AccountConfig, position_id: int, *, reason: str
) -> RecallResult:
    """살아 있는 진입 다리 전부 취소 요청 → `get_order_state` 로 확정(취소 중 추가 체결분은
    `filled_qty` 로 반영). 주문번호 없는 `sending|unknown` 다리는 건드리지 않는다(복구 틱이 확정).
    paused·off·dry_run 이면 취소 요청도 내지 않는다."""
    conn = ctx.conn
    rr = RecallResult()
    legs = [x for x in store.live_legs(conn, position_id=position_id) if is_entry(x.intent)]
    if not legs:
        return rr
    mode, _ = effective_mode(conn, account)
    hold = _record_only(ctx, mode, control.is_paused(conn))
    if hold is not None:
        rr.pending = len(legs)
        rr.errors.append(f"회수 보류({hold})")
        return rr
    broker = ctx.brokers.get(account.id)
    if broker is None:
        rr.pending = len(legs)
        rr.errors.append("broker 없음")
        return rr
    _, today = _today(ctx)
    pos = store.get_position(conn, position_id)
    for leg in legs:
        if leg.status == "prepared":
            with store.tx(conn):
                ok = store.cancel_prepared(conn, leg.id)  # 아직 prepared 일 때만(G2-2)
                if ok:
                    store.release_if_empty(conn, position_id, "회수: 전송 전 진입 다리 취소")
            if ok:
                rr.cancelled += 1
                continue
            cur_leg = store.get_leg(conn, leg.id)  # 전송기가 선점했다 — 현재 상태로 본다
            if cur_leg is None or cur_leg.order_no is None:
                rr.pending += 1
                continue
            leg = cur_leg
        if leg.order_no is None:
            rr.pending += 1
            continue
        try:
            broker.cancel_order(leg.order_no, qty=0)
        except Exception as e:  # noqa: BLE001 — 취소 거부여도 상태 조회로 확정한다
            rr.errors.append(f"취소 {leg.order_no}: {type(e).__name__}: {e}")
        try:
            st = broker.get_order_state(leg.order_no, today)
        except Exception as e:  # noqa: BLE001
            rr.errors.append(f"상태 {leg.order_no}: {type(e).__name__}: {e}")
            rr.pending += 1
            continue
        with store.tx(conn):
            if st.filled_qty > leg.filled_qty:
                rr.confirmed_filled += store.record_fill_progress(conn, leg.id, st.filled_qty)
            if st.terminal:
                status = "filled" if st.state == "filled" else st.state
                store.set_leg(conn, leg.id, status=status)
                if status == "cancelled":
                    rr.cancelled += 1
                store.release_if_empty(conn, position_id, f"회수: 진입 다리 {leg.id} {status}")
            else:
                rr.pending += 1
    if pos is not None and (rr.cancelled or rr.confirmed_filled):
        _notify(
            ctx,
            messages.fo_recall(
                account=account.id,
                code=pos.code,
                name=_name_of(ctx, pos.code),
                reason=reason,
                cancelled=rr.cancelled,
            ),  # fmt: skip
        )
    return rr


def _broker_qty(balance: FoBalance, pos: FoPositionRow) -> int | None:
    """브로커 잔고에서 이 포지션(코드·방향)의 확정 수량. 행이 없으면 None."""
    for p in balance.positions:
        if p.code == pos.code and p.contract_side == pos.contract_side:
            return p.qty
    return None


def _resize_target_reached(conn: sqlite3.Connection, cur: FoPositionRow) -> bool:
    """마지막 축소 다리가 감액(RESIZE_DOWN)이고 보유 ≤ 목표(> 0) 이면 True — 전량 청산 의도의
    exit_failed 는 해당 없음."""
    legs = store.legs_of(conn, cur.id)
    last = next((x for x in reversed(legs) if is_reduce(x.intent)), None)
    return (
        last is not None
        and last.intent == "RESIZE_DOWN"
        and 0 < cur.qty_target
        and cur.qty_held <= cur.qty_target
    )


def finalize(ctx: FoExecContext, pos: FoPositionRow) -> dict[str, Any]:
    """살아 있는 다리가 없을 때 상태를 정리한다: 보유 0 이면 closed, 보유 > 0 이면 held(감액이
    `exit_failed` 까지 갔다가 목표에 닿은 경우 포함, G2-7). 전량 청산의 `exit_failed`·`closing`
    잔량은 그대로 두어 재시도가 이어진다."""
    conn = ctx.conn
    cur = store.get_position(conn, pos.id)
    if cur is None:
        return {"position_id": pos.id, "status": "missing"}
    live = store.live_legs(conn, position_id=pos.id)
    if live:
        return {"position_id": pos.id, "status": cur.status, "live_legs": len(live)}
    changes: dict[str, Any] = {}
    if cur.qty_held == 0 and cur.status in ("entering", "resizing", "closing", "exit_failed"):
        changes = {"status": "closed", "closed_at": now_iso(), "active_reduce_leg_id": None}
    elif cur.qty_held > 0 and (
        cur.status in ("entering", "resizing")
        or (cur.status == "exit_failed" and _resize_target_reached(conn, cur))
    ):
        changes = {"status": "held", "active_reduce_leg_id": None}
    if changes:
        with store.tx(conn):
            store.transition(conn, cur.id, expected_version=cur.version, **changes)
    cur = store.get_position(conn, pos.id)
    assert cur is not None
    return {"position_id": pos.id, "status": cur.status, "qty_held": cur.qty_held}


def reduce(
    ctx: FoExecContext,
    account: AccountConfig,
    pos: FoPositionRow,
    *,
    intent: str,
    qty: int | None,
    reason: str,
    round: int = 0,
    stage: str | None = None,
) -> dict[str, Any]:
    """§5 청산·축소 절차: 진입 회수 → 잔고 재조회로 잔량 확정 → 축소 다리 1개(첫 시도 지정가,
    재시도는 시장가). 다음 틱은 `continue_reduce`. `qty=None` 은 전량(RESIZE_DOWN 은 목표
    초과분)."""
    conn, cfg = ctx.conn, ctx.settings.fo
    _, today = _today(ctx)
    stage = stage or f"_fo_order_{account.id}_{intent.lower()}_{pos.id}"
    mode, _ = effective_mode(conn, account)
    hold = _record_only(ctx, mode, control.is_paused(conn))
    if hold is not None:
        r = {"position_id": pos.id, "intent": intent, "status": "recorded", "record_only": hold,
             "reason": reason}  # fmt: skip
        _ckpt(ctx, today, stage, r)
        return r
    if pos.status == "unknown":
        return {"position_id": pos.id, "intent": intent, "status": "blocked",
                "note": "unknown 포지션은 사람만 정리"}  # fmt: skip
    rec = recall_entries(ctx, account, pos.id, reason=reason)
    cur = store.get_position(conn, pos.id)
    assert cur is not None
    if rec.pending:
        return {"position_id": pos.id, "intent": intent, "status": "blocked",
                "note": f"미확정 진입 다리 {rec.pending}건", "recall": rec.__dict__}  # fmt: skip
    # 잔고 재조회 — 브로커가 진실. 원장과 다르면 먼저 이 포지션 다리들의 미반영 체결을 다리에
    # 반영하고(수량은 다리 진행으로만 바뀐다 — 잔고 수량 덮어쓰기는 뒤의 체결 동기화가 이중
    # 차감한다, 단계 2 리뷰 S2-11) 그래도 다르면 unknown(스펙 §5 재시작 복구와 같다)
    broker = ctx.brokers.get(account.id)
    if broker is not None:
        try:
            bal = broker.get_balance()
        except Exception as e:  # noqa: BLE001 — 잔고 실패면 마지막 확정 잔량으로 간다
            log.warning("파생 잔고 재조회 실패(원장 잔량으로 진행): %s", e)
        else:
            held = _broker_qty(bal, cur)
            if (held or 0) != cur.qty_held:
                if not _apply_position_fills(ctx, broker, cur, today):
                    return {"position_id": pos.id, "intent": intent, "status": "blocked",
                            "note": "체결 조회 실패 — 잔고 불일치 미확정"}  # fmt: skip
                cur = store.get_position(conn, pos.id)
                assert cur is not None
            if (held or 0) != cur.qty_held and not (held is None and cur.qty_held == 0):
                if cur.status != "unknown":
                    with store.tx(conn):
                        store.transition(
                            conn, cur.id, expected_version=cur.version, status="unknown",
                            note=f"잔고 불일치: 원장 {cur.qty_held} vs 브로커 {held}",
                        )  # fmt: skip
                _notify(
                    ctx,
                    messages.fo_position_mismatch(
                        account=account.id,
                        code=cur.code,
                        name=_name_of(ctx, cur.code),
                        ledger_qty=cur.qty_held,
                        broker_qty=held,
                    ),  # fmt: skip
                )
                return {"position_id": pos.id, "intent": intent, "status": "unknown"}
    if cur.qty_held == 0:
        return finalize(ctx, cur)
    if intent == "RESIZE_DOWN":
        want = max(cur.qty_held - cur.qty_target, 0) if qty is None else qty
    else:
        want = cur.qty_held if qty is None else qty
    n = min(want, cur.qty_held)
    if n <= 0:
        return finalize(ctx, cur)
    attempt = store.next_attempt(conn, cur.id, intent, round)
    if attempt > cfg.exit_retry_max and cur.status != "exit_failed":
        with store.tx(conn):
            store.transition(conn, cur.id, expected_version=cur.version, status="exit_failed")
        cur = store.get_position(conn, pos.id)
        assert cur is not None
        log.critical("파생 청산 실패: 포지션 %s 시도 %s회 초과", cur.id, cfg.exit_retry_max)
        _notify(
            ctx,
            messages.fo_exit_failed(
                account=account.id,
                code=cur.code,
                name=_name_of(ctx, cur.code),
                qty=cur.qty_held,
                attempts=attempt - 1,
            ),  # fmt: skip
        )
    quote = quote_of(ctx, cur.code, cur.kind)
    leg = rules.reduce_leg(cfg, quote, cur, intent, n, market=attempt > 1)
    return _send_reduce(
        ctx, account, cur, leg, round=round, stage=stage, mode=mode, today=today, reason=reason
    )


def _apply_position_fills(
    ctx: FoExecContext, broker: Any, pos: FoPositionRow, today: date
) -> bool:
    """오늘 체결 조회로 이 포지션의 주문번호 있는 다리들의 누적 체결을 다리 진행에 반영한다(경제값은
    체결 동기화가 `fills` 행 기준으로 따로). **오늘 전송한 다리만** 본다 — KIS 주문번호는 날마다
    재시작하므로 옛 다리에 오늘 번호가 붙지 않게(G2-3, `fills._leg_for` 와 같은 날짜 범위). 조회
    실패면 False. 계보 모순(ValueError)은 경고만."""
    try:
        fills = broker.get_fills(today)
    except Exception as e:  # noqa: BLE001
        log.warning("파생 체결 조회 실패(잔고 대조 보류): %s", e)
        return False
    by_no: dict[str, int] = {}
    for f in fills:
        key = f.orgn_order_no or f.order_no
        by_no[key] = max(by_no.get(key, 0), f.filled_qty)
    lo, hi = repo.day_bounds(today, "KR")
    for leg in store.legs_of(ctx.conn, pos.id):
        if leg.order_no is None or leg.order_no not in by_no:
            continue
        at = leg.accepted_at or leg.sent_at or leg.created_at
        if at < lo or at >= hi:
            continue
        filled = by_no[leg.order_no]
        if filled <= leg.filled_qty:
            continue
        try:
            with store.tx(ctx.conn):
                store.record_fill_progress(ctx.conn, leg.id, filled)
        except ValueError as e:
            log.error("파생 다리 %s 체결 반영 불가: %s", leg.id, e)
    return True


def _resume_reduce(
    ctx: FoExecContext, account: AccountConfig, cur: FoPositionRow, leg: Any
) -> dict[str, Any]:
    """활성 축소 다리가 끝났는데(거부·취소·만료) 잔량이 목표보다 많으면 다시 낸다(단계 2 리뷰
    S2-4·C2-3). 감액(RESIZE_DOWN)은 `exit_failed` 로 격상된 뒤에도 **목표 수량을 유지한 채**
    보유 − 목표만 다시 내고(G2-7), 전량 청산 의도는 잔량 전부. 목표에 닿았으면 상태 정리."""
    intent = leg.intent if leg is not None else "CLOSE"
    rnd = leg.round if leg is not None else 0
    if intent == "RESIZE_DOWN":
        if cur.status in ("resizing", "exit_failed") and cur.qty_held > cur.qty_target:
            return reduce(ctx, account, cur, intent="RESIZE_DOWN", qty=None,
                          reason="재시도(다리 종료)", round=rnd)  # fmt: skip
        return finalize(ctx, cur)
    if cur.qty_held > 0 and cur.status in ("closing", "exit_failed"):
        return reduce(ctx, account, cur, intent=intent, qty=None, reason="재시도(다리 종료)",
                      round=rnd)  # fmt: skip
    return finalize(ctx, cur)


def continue_reduce(
    ctx: FoExecContext, account: AccountConfig, pos: FoPositionRow
) -> dict[str, Any]:
    """다음 틱: 활성 축소 다리가 미체결이면 취소 → 확정 → 실제 잔량만 시장가 `attempt+1`. 다리가
    끝났는데 잔량이 남았으면 다시 내고(`_resume_reduce`), 목표에 닿았으면 상태 정리(`finalize`).
    브로커 조회는 쓰기 잠금 밖."""
    conn = ctx.conn
    _, today = _today(ctx)
    cur = store.get_position(conn, pos.id)
    if cur is None:
        return {"position_id": pos.id, "status": "missing"}
    leg = store.get_leg(conn, cur.active_reduce_leg_id) if cur.active_reduce_leg_id else None
    mode, _ = effective_mode(conn, account)
    hold = _record_only(ctx, mode, control.is_paused(conn))
    if leg is None or not leg.live:
        if hold is not None:
            return finalize(ctx, cur)
        return _resume_reduce(ctx, account, cur, leg)
    if hold is not None:
        return {"position_id": cur.id, "status": "recorded", "record_only": hold, "leg_id": leg.id}
    broker = ctx.brokers.get(account.id)
    if broker is None:
        return {"position_id": cur.id, "status": "blocked", "note": "broker 없음"}
    if leg.status in ("sending", "unknown") or leg.order_no is None:
        try:
            fills = broker.get_fills(today)
        except Exception:  # noqa: BLE001
            fills = None
        res = positions._confirm_leg(conn, leg, broker.get_order_state, fills, today)
        if res != "confirmed":
            return {"position_id": cur.id, "status": "blocked", "note": f"미확정 다리 {leg.id}"}
        leg = store.get_leg(conn, leg.id)
        assert leg is not None
        if not leg.live:
            cur = store.get_position(conn, pos.id)
            assert cur is not None
            return _resume_reduce(ctx, account, cur, leg)
    # accepted·미체결: 취소 → 확정
    try:
        broker.cancel_order(leg.order_no, qty=0)
    except Exception as e:  # noqa: BLE001
        log.warning("파생 축소 다리 %s 취소 요청 실패(상태 조회로 확정): %s", leg.id, e)
    try:
        st = broker.get_order_state(leg.order_no, today)
    except Exception as e:  # noqa: BLE001
        return {"position_id": cur.id, "status": "pending", "note": f"상태 조회 실패: {e}"}
    with store.tx(conn):
        if st.filled_qty > leg.filled_qty:
            store.record_fill_progress(conn, leg.id, st.filled_qty)
        if st.terminal:
            store.set_leg(conn, leg.id, status="filled" if st.state == "filled" else st.state)
    if not st.terminal:
        return {"position_id": cur.id, "status": "pending", "leg_id": leg.id}
    cur = store.get_position(conn, pos.id)
    assert cur is not None
    if cur.qty_held == 0 or (leg.intent == "RESIZE_DOWN" and cur.qty_held <= cur.qty_target):
        return finalize(ctx, cur)
    return reduce(ctx, account, cur, intent=leg.intent, qty=None, reason="재시도", round=leg.round)
