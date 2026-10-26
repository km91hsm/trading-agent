"""노는 현금 단기금리 ETF 운용(Stock D-115 교훈, **기본 끔**): Stock 은 현금 대신 CD금리 ETF 를
들어 네 구간 모두 Sharpe·MDD 가 나아졌지만, 매수 자금이 필요할 때 동시호가 매도 대금이 늦게 풀려
2차 주문이 필요했다. 그래서 여기서는 **하락 국면(신규 매수가 막힌 동안)에만** 남는 현금을 산다 —
LLM 매수와 현금을 다투지 않는다. 국면이 bear 를 벗어나면 전량 판다.

monitor 틱에서 헤지 뒤 계좌별 운영일 1회(`control.cash_park_done:KR:<date>` = {계좌: "done"|시도
수}), 국내만. **auto 계좌만**(승인 대기 동안 국면 재판정이 요청을 만료시키는 등 승인 흐름과 맞지
않는다), dry_run 은 기록하지 않고, 거부·실패는 `MAX_ATTEMPTS` 까지 다음 틱에 다시.
주문은 `executor.execute(is_park=True)`(48_/58_, 결정 행 없음, 계좌 모드·LIVE 확인·paused·dry_run
그대로, 게이트는 헤지처럼 면제·비중 상한만). 분석·monitor·종목 수·섹터에서는
`Settings.excluded_tickers` 로 빠진다."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import Any

from trader.agents.schemas import Decision
from trader.control import control
from trader.ledger.repo import DEAD_STATUSES
from trader.orchestrator import executor

logger = logging.getLogger("trader.orchestrator")

MARKET = "KR"
MAX_ATTEMPTS = 3


def done_key(today: date) -> str:
    return f"cash_park_done:{MARKET}:{today.isoformat()}"


def _decision(ticker: str, action: str, weight: float, price: Decimal, why: str) -> Decision:
    # LLM 결정 스키마의 종목당 비중 상한(25%)은 현금 대용 ETF 에 맞지 않는다 — 파이썬이 만든 값이라
    # 검증 없이 만들고 실제 상한은 게이트의 park_max_weight 가 본다
    return Decision.model_construct(
        ticker=ticker,
        action=action,  # type: ignore[arg-type]
        target_weight=weight,
        order_type="LIMIT",
        limit_price=float(price),
        stop_loss=None,
        take_profit=None,
        horizon_days=5,
        confidence=1.0,
        rationale=f"현금 단기금리 ETF 운용 — {why} (파이썬 규칙, Stock D-115)"[:500],
        invalidation_conditions=["국면이 bear 를 벗어나면 전량 매도(파이썬)"],
    )


def run(
    ctx: executor.ExecContext,
    *,
    market_client: Any,
    today: date,
    regime: str,
    dry_run: bool,
) -> dict:
    cfg = ctx.settings.cash_park
    if not cfg.enabled:
        return {"skipped": "꺼짐"}
    state: dict = control.get_flag(ctx.conn, done_key(today)) or {}
    todo = [
        a
        for a in ctx.accounts
        if MARKET in a.markets
        and ctx.brokers.get(a.id) is not None
        and executor.effective_mode(ctx.conn, a)[0] == "auto"
        and state.get(a.id) != "done"
        and int(state.get(a.id) or 0) < MAX_ATTEMPTS
    ]
    if not todo:
        return {"skipped": "오늘 이미 실행"}
    price = Decimal(str(market_client.get_price_kr(cfg.ticker).price))
    out: dict[str, Any] = {"regime": regime, "orders": []}
    all_ids = {a.id for a in ctx.accounts}
    for a in todo:
        broker = ctx.brokers[a.id]
        pf = executor.build_portfolio(ctx.conn, broker, a, MARKET, today)
        held = pf.positions.get(cfg.ticker)
        held_eval = held.eval_amount if held else Decimal(0)
        if regime == "bear":
            spare = (
                pf.cash_available - pf.today_order_amount - Decimal(str(cfg.reserve_pct)) * pf.nav
            )
            if spare < cfg.min_amount or pf.nav <= 0 or price <= 0:
                state[a.id] = "done"  # 오늘 운용할 현금 없음
                continue
            weight = float((held_eval + spare) / pf.nav)
            dec = _decision(
                cfg.ticker, "ADD" if held else "BUY", weight, price, "하락 국면이라 남는 현금 운용"
            )
            cap = min(1.0, weight)
        elif held is not None and held.qty > 0:
            dec = _decision(cfg.ticker, "SELL", 0.0, price, f"국면 {regime} — 운용 해제")
            cap = None
        else:
            state[a.id] = "done"
            continue
        res = executor.execute(
            ctx,
            dec,
            date=today,
            market=MARKET,
            price=price,
            dry_run=dry_run,
            decision_id=None,
            skip_accounts=frozenset(all_ids - {a.id}),
            is_park=True,
            park_max_weight=cap,
        )
        out["orders"].append({"account_id": a.id, "action": dec.action, "results": res})
        ok = any(
            r.get("approved")
            and not r.get("error")
            and (r.get("dry_run") or (r.get("order") or {}).get("status") not in DEAD_STATUSES)
            for r in res
        )
        state[a.id] = "done" if ok else int(state.get(a.id) or 0) + 1
    if not dry_run:  # dry_run 은 실제 실행을 막지 않게 기록하지 않는다
        control.set_flag(ctx.conn, done_key(today), state)
    return out
