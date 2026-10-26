"""국면 헤지 (Phase 17 H): bear 면 인버스 ETF 를 계좌 NAV 의 max_weight_pct 만큼 들고, 벗어나면
전량 판다.

LLM 은 관여하지 않는다. 결정은 파이썬이 `Decision` 으로 만들고(`decisions.source='hedge'`), 주문은
`executor.execute(is_hedge=True)` 로 계좌마다 모드(off/approve/auto·LIVE 확인·paused·dry_run)를
그대로 지킨다. 운영일에 1회(`control.hedge_done:<market>:<date>`)만 돌며 monitor 잡의 첫 창 안
틱이 부른다(개장 뒤 실시간 시세). 보유 중 상품(−1x/−2x) 교체는 기본으로 하지 않는다 — 진입 때
고른 것을 해제까지 든다. Phase 21 `switch_instruments=true` 면 bear 유지 중 판정(count≥3 ∧ leverage)
과 보유 상품이 `switch_streak_days` 연속 어긋날 때 전량 매도하고(action `switch`) 다음 운영일 진입
규칙이 새 상품을 산다(같은 날 매도·매수를 함께 내지 않는다 — 매도 현금을 확인할 수 없다).

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
from trader.notify import messages
from trader.orchestrator import executor
from trader.orchestrator import market_view as market_view_mod
from trader.risk import regime as regime_mod

logger = logging.getLogger("trader.hedge")

HORIZON_DAYS = 20


def done_key(market: str, today: date) -> str:
    return f"hedge_done:{market}:{today.isoformat()}"


def switch_key(market: str) -> str:
    return f"hedge_switch_streak:{market}"


def _switch_due(
    conn, market: str, today: date, cfg: HedgeCfg, *, held: set[str], want: HedgeInstrument | None
) -> HedgeInstrument | None:
    """보유 상품이 원하는 상품과 다른 날을 센다(운영일마다 1). streak ≥ switch_streak_days 면 원하는
    상품을 돌려주고(교체 매도 실행), 일치하면 플래그를 지운다."""
    key = switch_key(market)
    if want is None or held == {want.symbol}:
        if control.get_flag(conn, key) is not None:
            control.set_flag(conn, key, None)
        return None
    prev = control.get_flag(conn, key) or {}
    days = int(prev.get("days") or 0)
    if prev.get("want") != want.symbol:
        days = 0
    if prev.get("date") != today.isoformat():
        days += 1
    control.set_flag(conn, key, {"days": days, "want": want.symbol, "date": today.isoformat()})
    return want if days >= cfg.switch_streak_days else None


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
    keys = ("ma_below", "momentum_weak", "vol_high", "flow_weak", "agent_risk_off")
    on = [k for k in keys if sig.get(k)]
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


MAX_RETRIES = 3  # 실패한 계좌의 재시도 상한(운영일 안, monitor 틱마다 1회)


def _held_hedges(
    ctx: executor.ExecContext, market: str, tickers: set[str]
) -> tuple[dict[str, set[str]], set[str]]:
    """계좌별 보유 헤지 종목 + 잔고 조회에 실패한 계좌(보유를 몰라 이번엔 건너뛴다)."""
    held: dict[str, set[str]] = {}
    failed: set[str] = set()
    for a in ctx.accounts:
        if market not in a.markets:
            continue
        b = ctx.brokers.get(a.id)
        if b is None:
            continue
        try:
            held[a.id] = {
                p.ticker for p in b.get_positions(market) if p.ticker in tickers and p.qty > 0
            }
        except (KisApiError, ValueError) as e:
            logger.warning("헤지 보유 조회 실패 %s: %s", a.id, e)
            failed.add(a.id)
    return held, failed


def _failed_accounts(results: list[dict]) -> set[str]:
    """주문이 나가야 했는데 못 나간 계좌(시세·잔고·주문 실패).
    거부 판정·off·dry-run 은 실패가 아니다."""
    out: set[str] = set()
    for r in results:
        if "error" in r and r.get("account_id"):
            out.add(str(r["account_id"]))
        o = r.get("order")
        if o and not o.get("accepted") and o.get("status") != "unknown":
            out.add(str(r["account_id"]))
    return out


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
    prev = control.get_flag(ctx.conn, key) if not force else None
    if prev and not isinstance(prev, dict):
        return {"skipped": "오늘 이미 실행"}
    retry = int((prev or {}).get("retry") or 0)
    done_accounts = set((prev or {}).get("done") or [])
    if prev and (not (prev.get("pending")) or retry >= MAX_RETRIES):
        return {"skipped": "오늘 이미 실행"}
    state = regime_mod.load(ctx.conn, market) or {}
    st = str(state.get("state") or "bull")
    tickers = cfg.tickers(market)
    held_by, unknown = _held_hedges(ctx, market, tickers)
    held = set().union(*held_by.values()) if held_by else set()
    # 계좌별로 본다: 보유한 계좌는 진입하지 않고, 보유하지 않은 계좌는 해제하지 않는다.
    # 오늘 이미 성공한 계좌·잔고를 모르는 계좌는 건너뛴다
    skip_enter = frozenset(done_accounts | unknown | {a for a, h in held_by.items() if h})
    view = market_view_mod.load_view(ctx.conn, market, today)
    out: dict[str, Any] = {
        "market": market,
        "state": st,
        "held": sorted(held),
        "held_by": {a: sorted(h) for a, h in held_by.items()},
        "action": "none",
        "view": (
            {k: view.get(k) for k in ("stance", "hedge_weight", "leveraged")} if view else None
        ),
    }
    deferred = _risk_on_deferral(state, view, cfg)
    if st == "bear" and not held and deferred:
        # 전략가 RISK_ON(확신 ≥ 기준)이고 지표가 경계값(bear_min_signals)이면 오늘 진입을 보류한다.
        # 3/3 은 보류 불가. 해제 규칙은 그대로(제안이 팔게 하지 않는다)
        out.update(action="deferred", reason=deferred)
        if not dry_run:
            control.set_flag(
                ctx.conn, key, {"state": st, "action": "deferred", "done": [], "pending": []}
            )
            executor._notify_quietly(ctx, messages.event("헤지 보류", f"{market} {deferred}"))
        return out
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
                ctx,
                "BUY",
                inst,
                market,
                today,
                cfg,
                state,
                quote_fn,
                dry_run,
                weight,
                view,
                skip_accounts=skip_enter,
            )
    elif st == "bear" and any(
        not h for a, h in held_by.items() if a not in done_accounts | unknown
    ):
        # 일부 계좌만 들고 있다(어제 한 계좌가 실패): 없는 계좌만 진입
        inst = cfg.instrument(market, leveraged=False)
        for t in sorted(held):
            known = cfg.instruments.get(market, {})  # type: ignore[arg-type]
            inst = next((i for i in known.values() if i.symbol == t), inst)
        if inst is not None:
            weight = market_view_mod.clamp_weight(view, cfg)
            out.update(action="enter", ticker=inst.symbol, weight=weight)
            out["results"] = _execute(
                ctx,
                "BUY",
                inst,
                market,
                today,
                cfg,
                state,
                quote_fn,
                dry_run,
                weight,
                view,
                skip_accounts=skip_enter,
            )
    elif (
        st == "bear"
        and held
        and getattr(cfg, "switch_instruments", False)
        and (
            want := _switch_due(
                ctx.conn,
                market,
                today,
                cfg,
                held=held,
                want=cfg.instrument(
                    market,
                    leveraged=int(state.get("count") or 0) >= 3
                    and (view is None or bool(view.get("leveraged", True))),
                ),
            )
        )
        is not None
    ):
        # 상품 교체: 오늘은 보유분 전량 매도만, 진입은 다음 운영일(bear 유지 시) 규칙이 want 를 산다
        out.update(action="switch", ticker=want.symbol)
        out["results"] = []
        for t in sorted(held):
            known = cfg.instruments.get(market, {})  # type: ignore[arg-type]
            inst = next((i for i in known.values() if i.symbol == t), HedgeInstrument(symbol=t))
            holders = {a for a, h in held_by.items() if t in h} - done_accounts
            skip_exit = frozenset({a.id for a in ctx.accounts} - holders)
            out["results"] += _execute(
                ctx,
                "SELL",
                inst,
                market,
                today,
                cfg,
                state,
                quote_fn,
                dry_run,
                skip_accounts=skip_exit,
            )
        if not dry_run:
            control.set_flag(ctx.conn, switch_key(market), None)
            sold = ", ".join(sorted(held))
            executor._notify_quietly(
                ctx,
                messages.event(
                    "헤지 교체", f"{market} {sold} 매도 → 다음 운영일 {want.symbol} 진입"
                ),
            )
    elif st != "bear" and held:
        out["action"] = "exit"
        out["results"] = []
        for t in sorted(held):
            known = cfg.instruments.get(market, {})  # type: ignore[arg-type]
            inst = next((i for i in known.values() if i.symbol == t), None)
            if inst is None:
                inst = HedgeInstrument(symbol=t)
            holders = {a for a, h in held_by.items() if t in h} - done_accounts
            skip_exit = frozenset({a.id for a in ctx.accounts} - holders)
            out["results"] += _execute(
                ctx,
                "SELL",
                inst,
                market,
                today,
                cfg,
                state,
                quote_fn,
                dry_run,
                skip_accounts=skip_exit,
            )
    if not dry_run:
        results = out.get("results") or []
        failed = _failed_accounts(results) | unknown if out["action"] != "none" else set()
        touched = {str(r["account_id"]) for r in results if r.get("account_id")}
        flag = {
            "state": st,
            "action": out["action"],
            "done": sorted(done_accounts | (touched - failed)),
            "pending": sorted(failed),
            "retry": retry + 1 if failed else retry,
        }
        control.set_flag(ctx.conn, key, flag)
        if failed and flag["retry"] >= MAX_RETRIES:
            executor._notify_quietly(
                ctx,
                messages.event(
                    "헤지 보류",
                    f"{market} 계좌 {', '.join(sorted(failed))}",
                    f"{MAX_RETRIES}회 실패해 오늘은 더 시도하지 않아요.",
                ),
            )
        out["pending_accounts"] = sorted(failed)
    return out


def _risk_on_deferral(state: dict, view: dict | None, cfg: HedgeCfg) -> str | None:
    """bear 진입 보류 사유(Phase 19). 조건: agent_signal ∧ 오늘 제안 RISK_ON ∧ confidence ≥ 기준 ∧
    정량 지표 수 == bear_min_signals(경계) ∧ 3/3 아님. 아니면 None."""
    if not getattr(cfg, "agent_signal", False) or not view:
        return None
    if view.get("stance") != "RISK_ON":
        return None
    conf = float(view.get("confidence") or 0)
    if conf < float(getattr(cfg, "agent_min_confidence", 1.0)):
        return None
    sig = state.get("signals") or {}
    n = sum(int(bool(sig.get(k))) for k in ("ma_below", "momentum_weak", "vol_high"))
    if n >= 3 or n != cfg.bear_min_signals:
        return None
    return f"전략가 RISK_ON(확신 {conf:.2f}) · 지표 {n}/3 경계 — 오늘 진입 보류"


def _execute(
    ctx,
    action,
    inst,
    market,
    today,
    cfg,
    state,
    quote_fn,
    dry_run,
    weight=None,
    view=None,
    *,
    skip_accounts: frozenset[str] = frozenset(),
) -> list[dict]:
    targets = [a.id for a in ctx.accounts if market in a.markets and a.id not in skip_accounts]
    try:
        price = Decimal(str(quote_fn(inst)))
    except (KisApiError, ValueError) as e:
        logger.warning("헤지 시세 실패 %s %s: %s", market, inst.symbol, e)
        executor._notify_quietly(
            ctx, messages.failure("헤지 보류", f"{market} {inst.symbol} 시세", e)
        )
        return [{"error": f"시세 실패: {e}", "account_id": a} for a in targets] or [
            {"error": f"시세 실패: {e}"}
        ]
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
        skip_accounts=skip_accounts,
    )
    for r in results:
        o = r.get("order")
        if o and o.get("accepted"):
            executor._notify_quietly(
                ctx,
                messages.hedge(
                    entering=action == "BUY",
                    account=r["account_id"],
                    market=market,
                    label=executor._label(ctx, market, inst.symbol),
                    qty=r["qty"],
                    detail=(
                        f"국면 {state.get('state')} {state.get('count')}/3 · "
                        f"접수번호 {o.get('order_no')}"
                    ),
                ),
            )
        elif r.get("approval_id"):
            executor._notify_quietly(
                ctx,
                messages.hedge_approval(
                    approval_id=r["approval_id"],
                    account=r["account_id"],
                    market=market,
                    label=executor._label(ctx, market, inst.symbol),
                ),
            )
    return results
