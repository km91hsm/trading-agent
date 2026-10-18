"""계좌별 실행 (스펙 §6.2, R16): 결정 1개 → 계좌마다 리스크 판정 → 모드별 주문·승인·기록.

주문은 이 모듈 안에서만 나간다. 실전 계좌의 `auto` 는 control 테이블의 확인 기록
(`live_confirmed:<id>`, `control.set_account_mode(confirm="LIVE")` 가 남김) 없이는
`off` 로 취급한다.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trader.agents import schemas
from trader.agents.schemas import Decision
from trader.broker.errors import KisApiError
from trader.broker.models import OrderRequest, OrderResult
from trader.config import AccountConfig, MemorySettings, Mode, Settings
from trader.control import control
from trader.ledger import repo
from trader.notify.telegram import Notifier, NullNotifier
from trader.orchestrator.run_store import run_dir, write_checkpoint
from trader.risk import rules
from trader.risk import stance as stance_mod

logger = logging.getLogger("trader.executor")
_KST = ZoneInfo("Asia/Seoul")

APPROVAL_TTL = timedelta(minutes=30)
QUOTE_PRICE_KEY = "quote_price"  # approvals.order_json 에 덧붙이는 요청 시 현재가


@dataclass
class ExecContext:
    settings: Settings
    accounts: list[AccountConfig]
    conn: sqlite3.Connection
    brokers: dict[str, Any] = field(default_factory=dict)  # account_id → KisBroker | FakeBroker
    notifier: Notifier = field(default_factory=NullNotifier)
    state: Path | None = None
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    holiday_fn: Callable[[date], bool] | None = None  # 보유 거래일 계산용 (KIS 휴장일, 캐시)
    rl_sizer: Any | None = None  # ml.rl.shadow.Sizer (Phase 9, 모의 계좌·BUY/ADD 에만)


def _holidays_between(
    fn: Callable[[date], bool] | None, start: date | None, end: date
) -> set[date]:
    if fn is None or start is None or start >= end:
        return set()
    out: set[date] = set()
    d = start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            try:
                if fn(d):
                    out.add(d)
            except Exception:  # noqa: BLE001 - 휴장일 조회 실패는 개장일로 본다
                pass
    return out


def effective_mode(conn: sqlite3.Connection, account: AccountConfig) -> tuple[Mode, str | None]:
    """control 우선 모드. 실전 auto 는 확인 기록이 없으면 off 로 강등하고 사유를 돌려준다."""
    mode = control.get_account_mode(conn, account)
    if account.env == "real" and mode == "auto":
        if not control.get_flag(conn, f"live_confirmed:{account.id}"):
            return "off", "실전 auto 는 확인 기록(live_confirmed) 없이는 off 로 취급"
    return mode, None


def _update_breaker(
    conn,
    account: AccountConfig,
    market: str,
    today: date,
    nav: Decimal,
    settings: MemorySettings | None = None,
) -> None:
    """모의 계좌 NAV(최근 30일 + 오늘)·최근 VIX 변화로 그 시장의 메모리 서킷브레이커를 갱신한다."""
    from trader.memory import store

    try:
        series = repo.nav_history(conn, account.id, market, days=30)
        series = [s for s in series if s[0] != today.isoformat()] + [(today.isoformat(), nav)]
        store.circuit_breaker(
            conn,
            nav_series=series,
            vix_change=store.latest_vix_change(conn),
            settings=settings or MemorySettings(),
            market=market,
        )
    except Exception as e:  # noqa: BLE001 - 브레이커 갱신 실패는 판정을 막지 않는다
        logger.warning("서킷브레이커 갱신 실패 %s: %s", account.id, e)


def build_portfolio(
    conn: sqlite3.Connection,
    broker: Any,
    account: AccountConfig,
    market: str,
    today: date,
    memory_settings: MemorySettings | None = None,
) -> rules.Portfolio:
    """브로커 잔고·현금 + 원장 집계로 Portfolio 를 만들고 positions_snapshot 을 남긴다."""
    positions = {p.ticker: p for p in broker.get_positions(market)}
    cash = broker.get_cash(market)
    nav = cash.total + sum((p.eval_amount for p in positions.values()), Decimal(0))
    prev = repo.latest_nav_before(conn, account.id, today, market=market)
    today_pnl = float(nav / prev - 1) if prev else 0.0
    if account.env == "paper":
        _update_breaker(conn, account, market, today, nav, memory_settings)
    repo.snapshot_positions(
        conn,
        account_id=account.id,
        d=today,
        market=market,
        nav=nav,
        cash=cash.total,
        positions=[
            {
                "ticker": p.ticker,
                "market": p.market,
                "qty": p.qty,
                "avg_price": p.avg_price,
                "current_price": p.current_price,
                "eval_amount": p.eval_amount,
            }
            for p in positions.values()
        ],
    )
    return rules.Portfolio(
        account_id=account.id,
        market=market,
        nav=nav,
        cash_available=cash.available,
        positions=positions,
        today_pnl_pct=today_pnl,
        today_buy_tickers=repo.today_buy_tickers(conn, account.id, today, market=market),
        today_order_amount=repo.today_order_amount(conn, account.id, today, market=market),
        held_since=repo.held_since(conn, account.id),
        trades_this_week=repo.trades_this_week(conn, account.id, today, market=market),
    )


def _wrap(data: Any) -> dict:
    return {"schema_version": schemas.CHECKPOINT_SCHEMA_VERSION, "data": data, "call": None}


@dataclass
class _RlProposal:
    obs: list[float]
    proposed: float
    apply: bool
    note: str | None


def _rl_size(
    ctx: ExecContext,
    account: AccountConfig,
    decision: Decision,
    market: str,
    portfolio: rules.Portfolio,
    base: list[float] | None,
) -> tuple[Decision, _RlProposal | None, list[float] | None]:
    """RL 사이저 관측·제안 (모의 계좌·BUY/ADD 전용, 호출자가 조건을 건다).

    반환: (판정에 넘길 결정, 제안, 재사용할 base 관측). 기록은 게이트 판정 뒤 `_rl_record` 가 한다.
    실패는 경고 후 RL 없이 진행한다.
    """
    from trader.ml.rl import shadow

    sizer = ctx.rl_sizer
    try:
        if base is None:
            base = shadow.base_observation(
                ctx.state, ticker=decision.ticker, market=market, decision=decision
            )
        obs = shadow.observe(
            ctx.state,
            ticker=decision.ticker,
            market=market,
            decision=decision,
            portfolio=portfolio,
            nav_hist=repo.nav_history(ctx.conn, account.id, market, days=30),
            base=base,
        )
        proposed = shadow.propose(sizer, obs)
    except Exception as e:  # noqa: BLE001 - 실험 기능이 주문 판정을 막지 않는다
        logger.warning("RL 사이저 건너뜀 %s %s: %s", account.id, decision.ticker, e)
        return decision, None, base
    apply = sizer.cfg.mode == "apply"
    if not apply:
        return decision, _RlProposal(obs, proposed, False, None), base
    note = f"RL 사이저 적용: {decision.target_weight:.2f} → {proposed:.2f} (v{sizer.version})"
    sized = decision.model_copy(update={"target_weight": proposed})
    return sized, _RlProposal(obs, proposed, True, note), base


def _rl_record(
    ctx: ExecContext,
    account: AccountConfig,
    decision: Decision,
    decision_id: int,
    prop: _RlProposal,
    verdict: rules.RiskVerdict,
) -> rules.RiskVerdict:
    """게이트 판정 뒤 rl_shadow 기록. applied 는 apply 모드이면서 게이트가 승인했을 때만."""
    from trader.ml.rl import shadow

    applied = prop.apply and verdict.approved
    try:
        shadow.record(
            ctx.conn,
            decision_id=decision_id,
            account_id=account.id,
            model_version=ctx.rl_sizer.version,
            obs=prop.obs,
            llm_weight=decision.target_weight,
            proposed_weight=prop.proposed,
            applied=applied,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("RL 섀도 기록 실패 %s %s: %s", account.id, decision.ticker, e)
    if prop.note:
        return verdict.model_copy(update={"reasons": [prop.note, *verdict.reasons]})
    return verdict


def execute(
    ctx: ExecContext,
    decision: Decision,
    *,
    date: date,
    market: str,
    price: Decimal,
    exchange: str | None = None,
    dry_run: bool = True,
    is_stop_loss: bool = False,
    decision_id: int | None = None,
) -> list[dict]:
    """계좌마다 판정·기록·(모드별) 주문. 계좌 하나의 실패가 다른 계좌를 막지 않는다.

    is_stop_loss 면 보유기간·주 2회 규칙을 면제하고 approve 계좌도 승인 없이 즉시 낸다(사후 통보).
    """
    d = run_dir(date, market, decision.ticker, ctx.state)
    if decision_id is None:
        decision_id = repo.decision_id(ctx.conn, date, market, decision.ticker)
    # 손절은 오늘 분석의 40_/50_ 체크포인트를 덮어쓰지 않도록 별도 이름을 쓴다
    risk_stage = "45_stop_loss" if is_stop_loss else "40_risk_check"
    order_stage = "55_order_sl" if is_stop_loss else "50_order"
    paused = control.is_paused(ctx.conn)
    results: list[dict] = []
    rl_base: list[float] | None = None
    rl_eligible = (
        ctx.rl_sizer is not None
        and decision_id is not None
        and not is_stop_loss
        and not dry_run  # dry-run·FAKE 세션의 제안은 실험 집계를 오염시킨다
        and decision.action in ("BUY", "ADD")
    )
    for account in ctx.accounts:
        if market not in account.markets:
            continue
        rl_prop: _RlProposal | None = None
        mode, note = effective_mode(ctx.conn, account)
        broker = ctx.brokers.get(account.id)
        st = stance_mod.decide(repo.realized_pnl_5d(ctx.conn, account.id, date, market=market))
        try:
            if broker is None:
                raise KisApiError("NO_BROKER", f"계좌 {account.id} 브로커 없음(앱키 미설정)")
            portfolio = build_portfolio(
                ctx.conn, broker, account, market, date, memory_settings=ctx.settings.memory
            )
            sized = decision
            if rl_eligible and account.env == "paper":
                sized, rl_prop, rl_base = _rl_size(
                    ctx, account, decision, market, portfolio, rl_base
                )
            verdict = rules.evaluate(
                sized,
                portfolio,
                limits=ctx.settings.risk,
                stance=st,
                market=market,
                price=price,
                today=date,
                is_stop_loss=is_stop_loss,
                holidays=_holidays_between(
                    ctx.holiday_fn, portfolio.held_since.get(decision.ticker), date
                ),
            )
        except (KisApiError, ValueError) as e:
            verdict = rules.RiskVerdict(
                account_id=account.id,
                approved=False,
                adjusted_qty=0,
                reasons=[f"잔고 조회 실패로 판정 불가: {e}"],
            )
        # 멱등: 같은 결정·계좌에 살아 있는 주문·승인 요청이 있으면 다시 내지 않는다 (재실행·재시도).
        # 손절은 미승인 요청(pending)에 막히면 안 되므로 주문만 본다.
        if (
            verdict.approved
            and decision_id is not None
            and repo.has_open_order_or_approval(
                ctx.conn,
                decision_id,
                account.id,
                verdict.side,
                include_approvals=not is_stop_loss,
            )
        ):
            verdict = verdict.model_copy(
                update={
                    "approved": False,
                    "adjusted_qty": 0,
                    "reasons": verdict.reasons + ["이미 이 결정에 대한 주문/승인 요청이 존재"],
                }
            )
        if rl_prop is not None and decision_id is not None:
            verdict = _rl_record(ctx, account, decision, decision_id, rl_prop, verdict)
        if paused and verdict.approved:
            note = (note + "; " if note else "") + "일시정지 중: 판정만 기록, 주문·승인 요청 없음"
        verdict = verdict.model_copy(
            update={"mode": mode, "note": note, "stance": st, "dry_run": dry_run}
        )
        entry: dict[str, Any] = {
            "account_id": account.id,
            "mode": mode,
            "note": note,
            "stance": st,
            "approved": verdict.approved,
            "qty": verdict.adjusted_qty,
            "reasons": verdict.reasons,
            "dry_run": dry_run,
            "order": None,
            "approval_id": None,
        }
        write_checkpoint(d, f"{risk_stage}_{account.id}", _wrap(verdict.model_dump(mode="json")))
        if decision_id is not None:
            repo.insert_risk_check(
                ctx.conn,
                decision_id=decision_id,
                account_id=account.id,
                approved=verdict.approved,
                adjusted_qty=verdict.adjusted_qty,
                reasons=verdict.reasons,
            )
        if not verdict.approved or mode == "off" or dry_run or paused:
            results.append(entry)
            continue
        request = OrderRequest(
            ticker=decision.ticker,
            market=market,  # type: ignore[arg-type]
            side=verdict.side,  # type: ignore[arg-type]
            qty=verdict.adjusted_qty,
            order_type=verdict.order_type,  # type: ignore[arg-type]
            price=verdict.price,
            exchange=exchange,
        )
        if mode == "approve" and not is_stop_loss:
            entry["approval_id"] = _request_approval(
                ctx, account, decision_id, request, verdict, quote_price=price
            )
        else:
            entry["order"] = _place(
                ctx, d, account, broker, decision_id, request, mode, price, order_stage=order_stage
            )
        results.append(entry)
    return results


def _request_approval(
    ctx: ExecContext,
    account: AccountConfig,
    decision_id: int | None,
    request: OrderRequest,
    verdict: rules.RiskVerdict,
    *,
    quote_price: Decimal | None = None,
) -> int:
    """승인 요청 저장. 시장가는 금액을 모르므로 요청 시 현재가(quote_price)를 함께 남겨
    승인 뒤 주문의 amount(가능금액 차감)로 쓴다. OrderRequest 복원 때는 이 키를 뺀다."""
    now = ctx.now()
    order = request.model_dump(mode="json")
    if quote_price is not None:
        order[QUOTE_PRICE_KEY] = str(quote_price)
    aid = repo.create_approval(
        ctx.conn,
        decision_id=decision_id or 0,
        account_id=account.id,
        order=order,
        requested_at=now.isoformat(),
        expires_at=(now + APPROVAL_TTL).isoformat(),
    )
    price = f" @{request.price}" if request.price is not None else " 시장가"
    ctx.notifier.send(
        f"[승인 요청 #{aid}] {account.id} {request.side} {request.ticker} {request.qty}주{price}\n"
        f"사유: {'; '.join(verdict.reasons) or '한도 내'}\n"
        f"/approve {aid} 또는 /reject {aid} (30분 내)"
    )
    return aid


def _place(
    ctx: ExecContext,
    d: Path,
    account: AccountConfig,
    broker: Any,
    decision_id: int | None,
    request: OrderRequest,
    mode: Mode,
    quote_price: Decimal | None = None,
    order_stage: str = "50_order",
) -> dict:
    try:
        result = broker.place_order(request)
        status = "accepted" if result.accepted else "rejected"
    except KisApiError as e:
        result = OrderResult(order_no=None, accepted=False, message=f"{e.msg_cd}: {e.msg1}")
        status = "failed"
        logger.warning("주문 실패 %s %s: %s", account.id, request.ticker, e)
    except ValueError as e:  # 거래소 코드 누락·계좌 시장 불일치 등 요청 자체의 오류
        result = OrderResult(order_no=None, accepted=False, message=f"INVALID: {e}")
        status = "failed"
        logger.warning("주문 요청 오류 %s %s: %s", account.id, request.ticker, e)
    sizing_price = request.price if request.price is not None else quote_price
    # orders 행은 멱등의 근거(has_open_order_or_approval)라 접수 뒤 반드시 남아야 한다.
    # 기록이 실패하면 접수번호를 알림·CRITICAL 로그에 남기고 올린다
    # (조용히 묻히면 재실행 때 같은 주문을 다시 낸다).
    try:
        repo.insert_order(
            ctx.conn,
            decision_id=decision_id,
            account_id=account.id,
            ticker=request.ticker,
            market=request.market,
            side=request.side,
            qty=request.qty,
            order_type=request.order_type,
            price=request.price,
            status=status,
            order_no=result.order_no,
            message=result.message,
            mode=mode,
            created_at=ctx.now().isoformat(),
            amount=Decimal(request.qty) * sizing_price if sizing_price is not None else None,
            org_no=result.org_no,
        )
    except Exception as e:
        logger.critical(
            "주문 기록 실패 %s %s %s %d주 접수번호 %s: %s",
            account.id,
            request.side,
            request.ticker,
            request.qty,
            result.order_no,
            e,
        )
        _notify_quietly(
            ctx,
            f"[긴급: 주문 기록 실패] {account.id} {request.side} {request.ticker} {request.qty}주 "
            f"접수번호 {result.order_no or '-'} ({status}) — 원장에 없으니 수동 확인 필요: {e}",
        )
        raise OrderBookkeepingError(result.order_no, e) from e
    payload = {
        "request": request.model_dump(mode="json"),
        "result": result.model_dump(mode="json"),
        "mode": mode,
        "status": status,
    }
    # 체크포인트·알림 실패는 원장에 이미 기록됐으므로 경고만 남긴다
    try:
        write_checkpoint(d, f"{order_stage}_{account.id}", _wrap(payload))
    except OSError as e:
        logger.warning("주문 체크포인트 기록 실패 %s %s: %s", account.id, request.ticker, e)
    head = "[주문]" if result.accepted else "[주문 실패]"
    _notify_quietly(
        ctx,
        f"{head} {account.id} {request.side} {request.ticker} {request.qty}주 "
        f"({mode}) 접수번호 {result.order_no or '-'} {result.message}",
    )
    return payload["result"] | {"status": status}


class OrderBookkeepingError(RuntimeError):
    """브로커가 접수한 뒤 원장 기록이 실패했다. order_no 로 수동 대조한다."""

    def __init__(self, order_no: str | None, cause: Exception) -> None:
        super().__init__(f"주문 기록 실패 (접수번호 {order_no or '-'}): {cause}")
        self.order_no = order_no


def _notify_quietly(ctx: ExecContext, text: str) -> None:
    """알림 실패가 주문 흐름을 끊지 않도록 예외를 경고로 바꾼다."""
    try:
        ctx.notifier.send(text)
    except Exception as e:  # noqa: BLE001 - 알림 채널 장애는 주문 결과와 무관
        logger.warning("알림 전송 실패: %s", type(e).__name__)


# --- 승인 처리 (approve 모드) ---------------------------------------------------


def expire_approvals(ctx: ExecContext) -> list[int]:
    """만료 시각이 지난 pending 승인을 expired 로 바꾸고 id 목록을 돌려준다."""
    now = ctx.now()
    expired: list[int] = []
    for a in repo.pending_approvals(ctx.conn):
        if datetime.fromisoformat(a["expires_at"]) <= now:
            repo.decide_approval(ctx.conn, a["id"], "expired", decided_at=now.isoformat())
            expired.append(a["id"])
    return expired


def _take_pending(ctx: ExecContext, approval_id: int) -> tuple[dict | None, str | None]:
    a = repo.get_approval(ctx.conn, approval_id)
    if a is None:
        return None, f"승인 요청 #{approval_id} 없음"
    if a["status"] != "pending":
        return None, f"승인 요청 #{approval_id} 은 이미 처리됨: {a['status']}"
    if datetime.fromisoformat(a["expires_at"]) <= ctx.now():
        repo.decide_approval(ctx.conn, approval_id, "expired", decided_at=ctx.now().isoformat())
        return None, f"승인 요청 #{approval_id} 은 만료됨 (30분 초과)"
    return a, None


def approve(ctx: ExecContext, approval_id: int) -> dict:
    """pending·미만료 승인 요청을 주문으로 실행한다. 두 번째 호출은 거부된다."""
    a, err = _take_pending(ctx, approval_id)
    if a is None:
        return {"ok": False, "error": err}
    if control.is_paused(ctx.conn):
        return {"ok": False, "error": "일시정지 중이라 승인을 실행하지 않음 (요청은 pending 유지)"}
    account = next((x for x in ctx.accounts if x.id == a["account_id"]), None)
    if account is None:
        return {"ok": False, "error": f"계좌 {a['account_id']} 가 설정에 없음"}
    order = dict(a["order"])
    raw_quote = order.pop(QUOTE_PRICE_KEY, None)
    quote_price = Decimal(str(raw_quote)) if raw_quote is not None else None
    request = OrderRequest.model_validate(order)
    dec = repo.get_decision(ctx.conn, a["decision_id"]) if a["decision_id"] else None
    day = date.fromisoformat(dec["date"]) if dec else ctx.now().astimezone(_KST).date()
    d = run_dir(day, request.market, request.ticker, ctx.state)
    broker = ctx.brokers.get(account.id)
    if broker is None:
        return {
            "ok": False,
            "error": f"계좌 {account.id} 브로커 없음(앱키 미설정): 요청은 pending 유지",
        }
    # 조건부 UPDATE: 다른 프로세스(telegram poll ↔ approvals approve)가 먼저 처리했으면 주문 없음
    if not repo.decide_approval(
        ctx.conn, approval_id, "approved", decided_at=ctx.now().isoformat()
    ):
        return {"ok": False, "error": f"승인 요청 #{approval_id} 은 이미 다른 프로세스가 처리함"}
    try:
        result = _place(
            ctx, d, account, broker, a["decision_id"] or None, request, "approve", quote_price
        )
    except OrderBookkeepingError as e:  # 접수 뒤 기록 실패: approved 로 굳지 않게 failed 로 남긴다
        repo.decide_approval(ctx.conn, approval_id, "failed", expect="approved")
        return {
            "ok": False,
            "approval_id": approval_id,
            "error": f"주문 기록 실패(접수번호 {e.order_no or '-'}): {e.__cause__}",
        }
    if not result["accepted"]:
        repo.decide_approval(ctx.conn, approval_id, "failed", expect="approved")
    return {"ok": bool(result["accepted"]), "approval_id": approval_id, "order": result}


def reject(ctx: ExecContext, approval_id: int) -> dict:
    a, err = _take_pending(ctx, approval_id)
    if a is None:
        return {"ok": False, "error": err}
    repo.decide_approval(ctx.conn, approval_id, "rejected", decided_at=ctx.now().isoformat())
    ctx.notifier.send(
        f"[거부] 승인 요청 #{approval_id} {a['account_id']} {a['order'].get('ticker')}"
    )
    return {"ok": True, "approval_id": approval_id}
