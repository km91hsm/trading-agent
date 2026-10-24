"""국면 헤지 (Phase 17 H): bear 면 인버스 ETF 를 계좌 NAV 의 max_weight_pct 만큼 들고, 벗어나면
전량 판다.

LLM 은 관여하지 않는다. 결정은 파이썬이 `Decision` 으로 만들고(`decisions.source='hedge'`), 주문은
`executor.execute(is_hedge=True)` 로 계좌마다 모드(off/approve/auto·LIVE 확인·paused·dry_run)를
그대로 지킨다. 운영일에 1회(`control.hedge_done:<market>:<date>`)만 돌며 monitor 잡의 첫 창 안
틱이 부른다(개장 뒤 실시간 시세). 보유 중 상품(−1x/−2x) 교체는 하지 않는다 — 진입 때 고른 것을
해제까지 든다.

Phase 18: 시장 세션의 제안(`market_view`, HedgeProposal)이 오늘 것으로 있으면 **bear 진입 비중**을
`[min_weight_pct, max_weight_pct]` 안에서 조절하고 레버리지를 거부(`leveraged=false`)할 수 있다.
진입 여부(bear)·해제는 제안과 무관하게 국면 규칙이 정한다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

from trader.agents.schemas import Decision
from trader.broker.errors import KisApiError
from trader.config import HedgeCfg, HedgeInstrument
from trader.control import control
from trader.ledger import repo
from trader.orchestrator import executor
from trader.orchestrator import market_view as market_view_mod
from trader.risk import regime as regime_mod

logger = logging.getLogger("trader.hedge")

HORIZON_DAYS = 20


def done_key(market: str, today: date) -> str:
    return f"hedge_done:{market}:{today.isoformat()}"


def hedge_decision(
    action: str,
    ticker: str,
    *,
    market: str,
    weight: float,
    price: Decimal,
    state: dict,
    view: dict | None = None,
) -> Decision:
    """파이썬이 만든 헤지 결정. KR 은 시장가, US 는 현재가 지정가(모의 서버 제약)."""
    sig = state.get("signals") or {}
    on = [k for k in ("ma_below", "momentum_weak", "vol_high") if sig.get(k)]
    note = ""
    if view and action == "BUY":
        pct = float(view.get("hedge_weight") or 0) * 100
        lev = "" if view.get("leveraged", True) else " 레버리지 거부"
        note = f"; 시장 세션 제안 {view.get('stance')} {pct:.0f}%{lev}"
    return Decision(
        ticker=ticker,
        action=action,  # type: ignore[arg-type]
        target_weight=weight if action == "BUY" else 0.0,
        order_type="MARKET" if market == "KR" else "LIMIT",
        limit_price=None if market == "KR" else float(price),
        stop_loss=None,
        take_profit=None,
        horizon_days=HORIZON_DAYS,
        confidence=1.0,
        rationale=(
            f"국면 {state.get('state')}({state.get('count')}/3: {', '.join(on) or '-'}) 헤지 "
            f"{'진입' if action == 'BUY' else '해제'} — 파이썬 규칙{note}"
        ),
        invalidation_conditions=["국면이 bear 를 벗어나면 전량 매도(파이썬)"],
    )


def _held_hedges(ctx: executor.ExecContext, market: str, tickers: set[str]) -> set[str]:
    """어느 계좌든 들고 있는 헤지 종목."""
    held: set[str] = set()
    for a in ctx.accounts:
        if market not in a.markets:
            continue
        b = ctx.brokers.get(a.id)
        if b is None:
            continue
        try:
            for p in b.get_positions(market):
                if p.ticker in tickers and p.qty > 0:
                    held.add(p.ticker)
        except (KisApiError, ValueError) as e:
            logger.warning("헤지 보유 조회 실패 %s: %s", a.id, e)
    return held


def rebalance(
    ctx: executor.ExecContext,
    *,
    market: str,
    today: date,
    cfg: HedgeCfg,
    quote_fn: Callable[[HedgeInstrument], Decimal],
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    """운영일 1회. 반환 {state, action: enter|exit|none, ticker, results|skipped}."""
    if not cfg.enabled:
        return {"skipped": "hedge.enabled=false"}
    key = done_key(market, today)
    if not force and control.get_flag(ctx.conn, key):
        return {"skipped": "오늘 이미 실행"}
    state = regime_mod.load(ctx.conn, market) or {}
    st = str(state.get("state") or "bull")
    tickers = cfg.tickers(market)
    held = _held_hedges(ctx, market, tickers)
    view = market_view_mod.load_view(ctx.conn, market, today)
    out: dict[str, Any] = {
        "market": market,
        "state": st,
        "held": sorted(held),
        "action": "none",
        "view": (
            {k: view.get(k) for k in ("stance", "hedge_weight", "leveraged")} if view else None
        ),
    }
    if st == "bear" and not held:
        leveraged = int(state.get("count") or 0) >= 3 and (
            view is None or bool(view.get("leveraged", True))
        )
        inst = cfg.instrument(market, leveraged=leveraged)
        if inst is None:
            out["error"] = f"{market} 헤지 상품 미설정"
        else:
            weight = market_view_mod.clamp_weight(view, cfg)
            out.update(action="enter", ticker=inst.symbol, weight=weight)
            out["results"] = _execute(
                ctx, "BUY", inst, market, today, cfg, state, quote_fn, dry_run, weight, view
            )
    elif st != "bear" and held:
        out["action"] = "exit"
        out["results"] = []
        for t in sorted(held):
            known = cfg.instruments.get(market, {})  # type: ignore[arg-type]
            inst = next((i for i in known.values() if i.symbol == t), None)
            if inst is None:
                inst = HedgeInstrument(symbol=t)
            out["results"] += _execute(
                ctx, "SELL", inst, market, today, cfg, state, quote_fn, dry_run
            )
    if not dry_run:
        control.set_flag(ctx.conn, key, {"state": st, "action": out["action"]})
    return out


def _execute(
    ctx, action, inst, market, today, cfg, state, quote_fn, dry_run, weight=None, view=None
) -> list[dict]:
    try:
        price = Decimal(str(quote_fn(inst)))
    except (KisApiError, ValueError) as e:
        logger.warning("헤지 시세 실패 %s %s: %s", market, inst.symbol, e)
        executor._notify_quietly(ctx, f"[헤지 보류] {market} {inst.symbol} 시세 실패: {e}")
        return [{"error": f"시세 실패: {e}"}]
    if price <= 0:
        return [{"error": "시세 0"}]
    dec = hedge_decision(
        action,
        inst.symbol,
        market=market,
        weight=cfg.max_weight_pct if weight is None else weight,
        price=price,
        state=state,
        view=view,
    )
    did = repo.insert_hedge_decision(ctx.conn, today, market, inst.symbol, dec.model_dump())
    results = executor.execute(
        ctx,
        dec,
        date=today,
        market=market,
        price=price,
        exchange=inst.exchange,
        dry_run=dry_run,
        decision_id=did,
        is_hedge=True,
    )
    for r in results:
        o = r.get("order")
        if o and o.get("accepted"):
            executor._notify_quietly(
                ctx,
                f"[헤지 {'진입' if action == 'BUY' else '해제'}] {r['account_id']} {market} "
                f"{executor._label(ctx, market, inst.symbol)} {r['qty']}주 "
                f"(국면 {state.get('state')} {state.get('count')}/3) 접수번호 {o.get('order_no')}",
            )
        elif r.get("approval_id"):
            executor._notify_quietly(
                ctx,
                f"[헤지 승인 요청] {r['account_id']} {market} {inst.symbol} #{r['approval_id']}",
            )
    return results
