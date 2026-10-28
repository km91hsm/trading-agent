"""노는 현금 단기금리 ETF 운용(Stock D-115 교훈, **기본 끔**): Stock 은 현금 대신 CD금리 ETF 를
들어 네 구간 모두 Sharpe·MDD 가 나아졌지만, 매수 자금이 필요할 때 동시호가 매도 대금이 늦게 풀려
2차 주문이 필요했다. 그래서 여기서는 **하락 국면(신규 매수가 막힌 동안)에만** 남는 현금을 산다 —
LLM 매수와 현금을 다투지 않는다. 국면이 bear 를 벗어나면 전량 판다.

monitor 틱에서 헤지 뒤 계좌별로 국내만. 상태는 `control.cash_park_done:KR:<date>` = {계좌:
{regime, status: "done"|시도 수, gen}} — **목표 행동(bear 면 매수, 아니면 매도)이 바뀔 때마다 새
세대**다(2-O17: 하루 1회 done 이 같은 날 국면 전환의 반대 주문을 막았다). 세대가 바뀌면 반대 방향의
당일 미체결 주문을 먼저 취소하고(못 하면 진행 중으로 남겨 다음 틱에 다시), 실행 키에 세대를 붙여
같은 날 다시 들어가도 첫 주문의 키에 막히지 않는다. **auto 계좌만**(승인 대기 동안 국면 재판정이
요청을 만료시키는 등 승인 흐름과 맞지 않는다), dry_run 은 기록하지 않고, 거부·실패는
`MAX_ATTEMPTS` 까지 다음 틱에 다시.
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
    for a in ctx.accounts:  # 세대가 바뀌면(목표 행동이 달라짐) 새로 시작
        cur = _entry(state.get(a.id))
        if cur["regime"] is not None and _target(cur["regime"]) != _target(regime):
            state[a.id] = {"regime": regime, "status": 0, "gen": cur["gen"] + 1}
        elif cur["regime"] is None and state.get(a.id) is not None:
            state[a.id] = {**cur, "regime": regime}  # 옛 형식("done"|수) — 오늘 처음 본 국면으로
    todo = [
        a
        for a in ctx.accounts
        if MARKET in a.markets
        and ctx.brokers.get(a.id) is not None
        and executor.effective_mode(ctx.conn, a)[0] == "auto"
        and _entry(state.get(a.id))["status"] != "done"
        and int(_entry(state.get(a.id))["status"] or 0) < MAX_ATTEMPTS
    ]
    if not todo:
        return {"skipped": "오늘 이미 실행"}
    price = Decimal(str(market_client.get_price_kr(cfg.ticker).price))
    out: dict[str, Any] = {"regime": regime, "orders": []}
    all_ids = {a.id for a in ctx.accounts}
    for a in todo:
        try:
            _one(ctx, a, cfg, price, regime, today, dry_run, all_ids, state, out)
        except Exception as e:  # noqa: BLE001 - 한 계좌의 잔고·주문 실패가 다른 계좌를 막지 않는다
            logger.warning("현금 운용 실패 %s: %s", a.id, e)
            _set(state, a.id, regime, _bump(state.get(a.id)))
            out["orders"].append({"account_id": a.id, "error": f"{type(e).__name__}: {e}"})
    if not dry_run:  # dry_run 은 실제 실행을 막지 않게 기록하지 않는다
        control.set_flag(ctx.conn, done_key(today), state)
    return out


def _target(regime: str | None) -> str:
    return "buy" if regime == "bear" else "sell"


def _entry(raw) -> dict:
    """계좌 상태 한 칸. 옛 형식("done"|시도 수)은 국면 미상으로 읽는다."""
    if isinstance(raw, dict):
        return {
            "regime": raw.get("regime"),
            "status": raw.get("status", 0),
            "gen": int(raw.get("gen") or 0),
        }
    return {"regime": None, "status": raw if raw is not None else 0, "gen": 0}


def _bump(raw) -> int:
    st = _entry(raw)["status"]
    return (0 if st == "done" else int(st or 0)) + 1


def _set(state: dict, acct: str, regime: str, status) -> None:
    cur = _entry(state.get(acct))
    state[acct] = {"regime": regime, "status": status, "gen": cur["gen"]}


def _one(ctx, a, cfg, price, regime, today, dry_run, all_ids, state, out) -> None:
    """계좌 하나: 반대 방향 미체결 취소 → 잔고 → (bear) 남는 현금 매수 / (그 밖) 보유분 매도.
    state·out 을 갱신한다."""
    broker = ctx.brokers[a.id]
    gen = _entry(state.get(a.id))["gen"]
    if not dry_run:
        opposite = "SELL" if regime == "bear" else "BUY"
        n, left = executor.cancel_open_buys(
            ctx.conn, broker, a.id, MARKET, today, ticker=cfg.ticker, side=opposite
        )
        if n or left:
            out["orders"].append(
                {"account_id": a.id, "action": f"cancel_{opposite}", "cancelled": n}
                | ({"cancel_failed": left} if left else {})
            )
        if left:  # 반대 주문이 남아 있으면 이번 틱은 여기까지 — 다음 틱에 다시 취소
            _set(state, a.id, regime, _bump(state.get(a.id)))
            return
    pf = executor.build_portfolio(
        ctx.conn, broker, a, MARKET, today, paper_ids=executor.paper_ids(ctx.accounts)
    )
    held = pf.positions.get(cfg.ticker)
    held_eval = held.eval_amount if held else Decimal(0)
    if regime == "bear":
        spare = pf.cash_available - pf.today_order_amount - Decimal(str(cfg.reserve_pct)) * pf.nav
        if spare < cfg.min_amount or pf.nav <= 0 or price <= 0:
            _set(state, a.id, regime, "done")  # 오늘 운용할 현금 없음
            return
        weight = float((held_eval + spare) / pf.nav)
        dec = _decision(
            cfg.ticker, "ADD" if held else "BUY", weight, price, "하락 국면이라 남는 현금 운용"
        )
        cap = min(1.0, weight)
    elif held is not None and held.qty > 0:
        dec = _decision(cfg.ticker, "SELL", 0.0, price, f"국면 {regime} — 운용 해제")
        cap = None
    else:
        _set(state, a.id, regime, "done")
        return
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
        exec_tag=f"g{gen}" if gen else None,
    )
    out["orders"].append({"account_id": a.id, "action": dec.action, "results": res})
    ok = any(
        r.get("approved")
        and not r.get("error")
        and (r.get("dry_run") or (r.get("order") or {}).get("status") not in DEAD_STATUSES)
        for r in res
    )
    _set(state, a.id, regime, "done" if ok else _bump(state.get(a.id)))
