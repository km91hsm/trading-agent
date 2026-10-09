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
    prev = repo.latest_nav_before(conn, account.id, today)
    today_pnl = float(nav / prev - 1) if prev else 0.0
    repo.snapshot_positions(
        conn,
        account_id=account.id,
        d=today,
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
        today_order_amount=repo.today_order_amount(conn, account.id, today),
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
) -> list[dict]:
    """계좌마다 판정·기록·(모드별) 주문. 계좌 하나의 실패가 다른 계좌를 막지 않는다."""
    d = run_dir(date, market, decision.ticker, ctx.state)
    decision_id = repo.decision_id(ctx.conn, date, market, decision.ticker)
    results: list[dict] = []
    for account in ctx.accounts:
        if market not in account.markets:
            continue
        mode, note = effective_mode(ctx.conn, account)
        broker = ctx.brokers.get(account.id)
        st = stance_mod.decide(repo.realized_pnl_5d(ctx.conn, account.id, date))
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
            )
        except (KisApiError, ValueError) as e:
            verdict = rules.RiskVerdict(
                account_id=account.id,
                approved=False,
                adjusted_qty=0,
                reasons=[f"잔고 조회 실패로 판정 불가: {e}"],
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
        write_checkpoint(
            d,
            f"40_risk_check_{account.id}",
            _wrap(
                {
                    **verdict.model_dump(mode="json"),
                    "mode": mode,
                    "note": note,
                    "stance": st,
                    "dry_run": dry_run,
                }
            ),
        )
        if decision_id is not None:
            repo.insert_risk_check(
                ctx.conn,
                decision_id=decision_id,
                account_id=account.id,
                approved=verdict.approved,
                adjusted_qty=verdict.adjusted_qty,
                reasons=verdict.reasons,
            )
        if not verdict.approved or mode == "off" or dry_run:
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
        if mode == "approve":
            entry["approval_id"] = _request_approval(ctx, account, decision_id, request, verdict)
        else:
            entry["order"] = _place(ctx, d, account, broker, decision_id, request, mode)
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
) -> dict:
    try:
        result = broker.place_order(request)
        status = "accepted" if result.accepted else "rejected"
    except KisApiError as e:
        result = OrderResult(order_no=None, accepted=False, message=f"{e.msg_cd}: {e.msg1}")
        status = "failed"
        logger.warning("주문 실패 %s %s: %s", account.id, request.ticker, e)
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
