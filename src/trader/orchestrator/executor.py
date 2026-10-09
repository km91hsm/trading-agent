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
from trader.config import AccountConfig, Mode, Settings
from trader.control import control
from trader.ledger import repo
from trader.notify.telegram import Notifier, NullNotifier
from trader.orchestrator.run_store import run_dir, write_checkpoint
from trader.risk import rules
from trader.risk import stance as stance_mod

logger = logging.getLogger("trader.executor")
_KST = ZoneInfo("Asia/Seoul")

APPROVAL_TTL = timedelta(minutes=30)


@dataclass
class ExecContext:
    settings: Settings
    accounts: list[AccountConfig]
    conn: sqlite3.Connection
    brokers: dict[str, Any] = field(default_factory=dict)  # account_id → KisBroker | FakeBroker
    notifier: Notifier = field(default_factory=NullNotifier)
    state: Path | None = None
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))


def effective_mode(conn: sqlite3.Connection, account: AccountConfig) -> tuple[Mode, str | None]:
    """control 우선 모드. 실전 auto 는 확인 기록이 없으면 off 로 강등하고 사유를 돌려준다."""
    mode = control.get_account_mode(conn, account)
    if account.env == "real" and mode == "auto":
        if not control.get_flag(conn, f"live_confirmed:{account.id}"):
            return "off", "실전 auto 는 확인 기록(live_confirmed) 없이는 off 로 취급"
    return mode, None


def build_portfolio(
    conn: sqlite3.Connection, broker: Any, account: AccountConfig, market: str, today: date
) -> rules.Portfolio:
    """브로커 잔고·현금 + 원장 집계로 Portfolio 를 만들고 positions_snapshot 을 남긴다."""
    positions = {p.ticker: p for p in broker.get_positions(market)}
    cash = broker.get_cash(market)
    nav = cash.total + sum((p.eval_amount for p in positions.values()), Decimal(0))
    prev = repo.latest_nav_before(conn, account.id, today, market=market)
    today_pnl = float(nav / prev - 1) if prev else 0.0
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
        today_buy_tickers=repo.today_buy_tickers(conn, account.id, today),
        today_order_amount=repo.today_order_amount(conn, account.id, today, market=market),
        held_since=repo.held_since(conn, account.id),
        trades_this_week=repo.trades_this_week(conn, account.id, today),
    )


def _wrap(data: Any) -> dict:
    return {"schema_version": schemas.CHECKPOINT_SCHEMA_VERSION, "data": data, "call": None}


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
) -> list[dict]:
    """계좌마다 판정·기록·(모드별) 주문. 계좌 하나의 실패가 다른 계좌를 막지 않는다.

    is_stop_loss 면 보유기간·주 2회 규칙을 면제하고 approve 계좌도 승인 없이 즉시 낸다(사후 통보).
    """
    d = run_dir(date, market, decision.ticker, ctx.state)
    decision_id = repo.decision_id(ctx.conn, date, market, decision.ticker)
    paused = control.is_paused(ctx.conn)
    results: list[dict] = []
    for account in ctx.accounts:
        if market not in account.markets:
            continue
        mode, note = effective_mode(ctx.conn, account)
        broker = ctx.brokers.get(account.id)
        st = stance_mod.decide(repo.realized_pnl_5d(ctx.conn, account.id, date, market=market))
        try:
            if broker is None:
                raise KisApiError("NO_BROKER", f"계좌 {account.id} 브로커 없음(앱키 미설정)")
            portfolio = build_portfolio(ctx.conn, broker, account, market, date)
            verdict = rules.evaluate(
                decision,
                portfolio,
                limits=ctx.settings.risk,
                stance=st,
                market=market,
                price=price,
                today=date,
                is_stop_loss=is_stop_loss,
            )
        except (KisApiError, ValueError) as e:
            verdict = rules.RiskVerdict(
                account_id=account.id,
                approved=False,
                adjusted_qty=0,
                reasons=[f"잔고 조회 실패로 판정 불가: {e}"],
            )
        # 멱등: 같은 결정·계좌에 살아 있는 주문·승인 요청이 있으면 다시 내지 않는다 (재실행·재시도)
        if (
            verdict.approved
            and decision_id is not None
            and repo.has_open_order_or_approval(ctx.conn, decision_id, account.id, verdict.side)
        ):
            verdict = verdict.model_copy(
                update={
                    "approved": False,
                    "adjusted_qty": 0,
                    "reasons": verdict.reasons + ["이미 이 결정에 대한 주문/승인 요청이 존재"],
                }
            )
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
        write_checkpoint(d, f"40_risk_check_{account.id}", _wrap(verdict.model_dump(mode="json")))
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
            entry["approval_id"] = _request_approval(ctx, account, decision_id, request, verdict)
        else:
            entry["order"] = _place(ctx, d, account, broker, decision_id, request, mode, price)
        results.append(entry)
    return results


def _request_approval(
    ctx: ExecContext,
    account: AccountConfig,
    decision_id: int | None,
    request: OrderRequest,
    verdict: rules.RiskVerdict,
) -> int:
    now = ctx.now()
    aid = repo.create_approval(
        ctx.conn,
        decision_id=decision_id or 0,
        account_id=account.id,
        order=request.model_dump(mode="json"),
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
    payload = {
        "request": request.model_dump(mode="json"),
        "result": result.model_dump(mode="json"),
        "mode": mode,
        "status": status,
    }
    write_checkpoint(d, f"50_order_{account.id}", _wrap(payload))
    head = "[주문]" if result.accepted else "[주문 실패]"
    ctx.notifier.send(
        f"{head} {account.id} {request.side} {request.ticker} {request.qty}주 "
        f"({mode}) 접수번호 {result.order_no or '-'} {result.message}"
    )
    return payload["result"] | {"status": status}


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
    request = OrderRequest.model_validate(a["order"])
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
    result = _place(ctx, d, account, broker, a["decision_id"] or None, request, "approve")
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
