"""아침 미체결 재호가 (Phase 23 B).

개장 뒤 `risk.reprice.after_min` 분에 운영일 1회(`control.reprice_done:<market>:<date>`): 오늘 낸
지정가 주문 중 체결이 0 인 `accepted` 를 **취소 → 같은 결정으로 현재가 지정가 재제출**한다.
재제출은 `executor.execute(is_reprice=True)` 라 게이트(비중·현금·중복·국면)를 다시 통과하고
체크포인트는 `42_reprice_<acct>`·`52_order_reprice_<acct>` 다. 취소된 원 주문은 dead 라 중복
가드에 걸리지 않는다. 원 지정가 대비 `max_chase_pct` 넘게 불리한(BUY 는 더 높은, SELL 은 더
낮은) 현재가면 재제출하지 않고 알림만. dry_run·`off` 계좌·unknown(번호 없음)·외부 주문은
건너뛴다."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

from trader.agents.schemas import Decision
from trader.broker.errors import KisApiError
from trader.config import RepriceCfg
from trader.control import control
from trader.ledger import repo
from trader.notify import messages
from trader.orchestrator import executor

logger = logging.getLogger("trader.reprice")


def done_key(market: str, today: date) -> str:
    return f"reprice_done:{market}:{today.isoformat()}"


def candidates(conn, account_id: str, market: str, today: date) -> list[dict]:
    """오늘 낸 지정가 accepted 주문 중 체결 0·결정 있음·외부 아님."""
    lo, hi = repo.day_bounds(today, market)
    rows = conn.execute(
        "SELECT o.id, o.decision_id, o.ticker, o.side, o.qty, o.price, o.order_no, o.org_no, "
        "o.mode, COALESCE((SELECT SUM(f.qty) FROM fills f WHERE f.order_id=o.id), 0) AS filled "
        "FROM orders o WHERE o.account_id=? AND o.market=? AND o.status='accepted' "
        "AND o.order_type='LIMIT' AND o.order_no IS NOT NULL AND o.decision_id IS NOT NULL "
        "AND o.mode != 'external' AND o.created_at >= ? AND o.created_at < ? ORDER BY o.id",
        (account_id, market, lo, hi),
    ).fetchall()
    return [dict(r) for r in rows if int(r["filled"] or 0) == 0 and r["price"] is not None]


def too_far(side: str, limit: Decimal, quote: Decimal, max_chase_pct: float) -> bool:
    """재제출을 포기할 만큼 불리한가: BUY 는 현재가 > 원 지정가 × (1+max), SELL 은 < (1−max)."""
    m = Decimal(str(max_chase_pct))
    if side == "BUY":
        return quote > limit * (1 + m)
    return quote < limit * (1 - m)


def reprice(
    ctx: executor.ExecContext,
    *,
    market: str,
    today: date,
    cfg: RepriceCfg,
    quote_fn: Callable[[str, str | None], Decimal],
    exchanges: dict[str, str] | None = None,
    dry_run: bool = False,
    label: Callable[[str], str] | None = None,
) -> dict:
    """운영일 1회. 반환 {repriced:[...], skipped:[...]}. 시각 조건은 호출자(데몬)가 본다."""
    if not cfg.enabled:
        return {"skipped": "reprice.enabled=false"}
    if dry_run:
        return {"skipped": "dry-run"}
    if control.get_flag(ctx.conn, done_key(market, today)):
        return {"skipped": "오늘 이미 실행"}
    if control.is_paused(ctx.conn):
        return {"skipped": "일시정지"}
    name = label or (lambda t: t)
    out: dict[str, Any] = {"repriced": [], "skipped": []}
    for account in ctx.accounts:
        if market not in account.markets:
            continue
        mode, _ = executor.effective_mode(ctx.conn, account)
        broker = ctx.brokers.get(account.id)
        if mode == "off" or broker is None:
            continue
        for row in candidates(ctx.conn, account.id, market, today):
            ticker = row["ticker"]
            exchange = (exchanges or {}).get(ticker)
            limit = Decimal(str(row["price"]))
            try:
                quote = Decimal(str(quote_fn(ticker, exchange)))
            except (KisApiError, ValueError) as e:
                out["skipped"].append({"order_id": row["id"], "why": f"시세 실패: {e}"})
                continue
            if quote <= 0:
                out["skipped"].append({"order_id": row["id"], "why": "시세 0"})
                continue
            if too_far(row["side"], limit, quote, cfg.max_chase_pct):
                why = f"현재가 {quote} 가 원 지정가 {limit} 대비 {cfg.max_chase_pct:.1%} 넘게 불리"
                out["skipped"].append({"order_id": row["id"], "why": why})
                ctx.notifier.send(
                    messages.reprice_deferred(
                        account=account.id,
                        market=market,
                        label=name(ticker),
                        side=row["side"],
                        qty=row["qty"],
                        limit=limit,
                        why=why,
                    )
                )
                continue
            try:
                res = broker.cancel_order(
                    str(row["order_no"]),
                    market=market,
                    org_no=row["org_no"],
                    ticker=ticker,
                    exchange=exchange,
                    qty=int(row["qty"]),
                )
            except (KisApiError, ValueError) as e:
                out["skipped"].append({"order_id": row["id"], "why": f"취소 실패: {e}"})
                logger.warning(
                    "재호가 취소 실패 %s %s #%s: %s", account.id, ticker, row["order_no"], e
                )
                continue
            if not res.accepted:
                out["skipped"].append({"order_id": row["id"], "why": f"취소 거부: {res.message}"})
                continue
            repo.set_order_status(ctx.conn, int(row["id"]), "cancelled")
            dec_row = repo.get_decision(ctx.conn, int(row["decision_id"]), hydrate=True)
            if dec_row is None:
                out["skipped"].append({"order_id": row["id"], "why": "결정 행 없음(취소만)"})
                continue
            dec = Decision(**dec_row["data"]).model_copy(
                update={"order_type": "LIMIT", "limit_price": float(quote)}
            )
            results = executor.execute(
                ctx,
                dec,
                date=today,
                market=market,
                price=quote,
                exchange=exchange,
                dry_run=False,
                decision_id=int(row["decision_id"]),
                skip_accounts=frozenset(a.id for a in ctx.accounts if a.id != account.id),
                extra_note=f"재호가 {limit}→{quote} (원 주문 #{row['id']} 취소)",
                is_reprice=True,
            )
            r0 = results[0] if results else {}
            out["repriced"].append(
                {
                    "order_id": row["id"],
                    "account_id": account.id,
                    "ticker": ticker,
                    "side": row["side"],
                    "from": str(limit),
                    "to": str(quote),
                    "approved": bool(r0.get("approved")),
                    "new_order": (r0.get("order") or {}).get("status"),
                }
            )
            tail = (
                "재제출"
                if r0.get("approved")
                else f"게이트 거부: {'; '.join(r0.get('reasons') or [])}"
            )
            ctx.notifier.send(
                messages.reprice(
                    account=account.id,
                    market=market,
                    label=name(ticker),
                    side=row["side"],
                    qty=row["qty"],
                    old=limit,
                    new=quote,
                    tail=tail,
                )
            )
    control.set_flag(ctx.conn, done_key(market, today), True)
    return out
