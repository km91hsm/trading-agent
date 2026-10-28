"""파생 모니터 엔진(Phase 26 단계 4, 스펙 §5 `engine.tick`). 데몬 `KR_fo_monitor`(파생 정규장
08:45~15:45, 60초)와 `trader fo monitor --once` 가 부른다. 계좌마다 순서:

① 복구·동기화 — `positions.recover`(다리 확정·잔고 대조) → 진행 중 축소는 `continue_reduce`,
   다리 없는 진입은 `finalize`, 정산 대기는 잔고 대조로 `settled`
② 보호선 — 손절(기초지수[지수 갱신 시간 ∧ 신선] **또는** 선물 가격 `stop_fut` **또는** 저장된
   옵션 프리미엄 보호선 `premium_stop`), 익절, 보유기간 만료, 만기 강제 청산(D-roll 09:00),
   `hold_overnight=false` 마감 `close_before_min` 전 청산
③ 유지 조건 — 실제 노출 배수·현금 버퍼·추가증거금·수명 예산은 매 틱, 시나리오 스트레스는 15:00
   확정 검사(그 전엔 알림만). 위반이면 축소 순서 옵션 매도 → 선물 → 옵션 매수
④ 일일 손실 → `fo_halted:<계좌>`(신규 금지) + 진입 회수. `enabled=false`·킬스위치·진입 cutoff
   뒤·검증 플래그 상실도 진입 회수
⑤ 트리거 → `triggers` 기록 + 재평가 요청 → 대기열 `fo_reevaluate`
⑥ 보류 진입(`fo_pending_entry`, 진입 창 안이면 `run_leg` 재계산) · REPLACE 두 번째 다리
⑦ NAV·보유 스냅샷(포지션이 없어도)

주문은 전부 `fo/executor`(reduce·run_leg·recall_entries) 를 지나고, 여기는 **언제 무엇을** 낼지만
정한다. paused·off·dry_run 은 executor 가 기록만으로 바꾼다."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from trader.broker.fo import CONTRACT_VERSION
from trader.broker.models import FoBalance, FoQuote
from trader.config import KST, AccountConfig, FoCfg
from trader.control import control
from trader.fo import balance as fo_balance
from trader.fo import executor, positions, reeval, rules, sizing, store, triggers
from trader.fo.executor import FoExecContext
from trader.fo.schemas import FoDecisionEnvelope
from trader.fo.types import FoPositionRow, is_entry
from trader.ledger import repo
from trader.ledger.db import now_iso
from trader.market_hours import fo_close_hm, index_live
from trader.notify import messages
from trader.risk import horizon, killswitch

log = logging.getLogger("trader.fo")

FORCE_CLOSE_AT = time(9, 0)
STRESS_CONFIRM_AT = time(15, 0)
REDUCE_ORDER = ("OPTION_SELL", "FUTURES", "OPTION_BUY")  # 유지 조건 축소 순서
ALERT_FLAG = "fo_alerts:"

RequestFn = Callable[[FoExecContext, dict[str, Any]], Any]


def _enqueue(ctx: FoExecContext, params: dict[str, Any]) -> Any:
    """기본 재평가 요청: 빠른 대기열 `fo_reevaluate`(데몬이 실행). 중복은 무시."""
    from trader import jobs

    try:
        return jobs.enqueue(ctx.conn, "fo_reevaluate", params, source="daemon")
    except jobs.DuplicateRequest:
        return None
    except Exception as e:  # noqa: BLE001 — 요청 실패는 트리거 처리 실패로 남기고 감시는 계속
        log.warning("파생 재평가 대기열 등록 실패: %s", e)
        return None


def _notify_once(ctx: FoExecContext, account_id: str, key: str, text: str, today: date) -> None:
    """같은 운영일·같은 키의 알림은 한 번만(기록만 모드에서 매 틱 같은 청산 알림이 쌓이지 않게)."""
    flag_key = f"{ALERT_FLAG}{account_id}"
    flag = control.get_flag(ctx.conn, flag_key) or {}
    sent = list(flag.get("keys") or []) if flag.get("date") == today.isoformat() else []
    if key in sent:
        return
    control.set_flag(ctx.conn, flag_key, {"date": today.isoformat(), "keys": [*sent, key][-200:]})
    executor._notify(ctx, text)


# --- 보호선(순수) ------------------------------------------------------------------------


def exit_reason(
    pos: FoPositionRow,
    *,
    cfg: FoCfg,
    index: Decimal | None,
    quote: FoQuote | None,
    now: datetime,
    today: date,
    holiday_fn: Callable[[date], bool] | None = None,
) -> tuple[str, str] | None:
    """보호선 판정 → (청산 의도, 사유) 또는 None. `index` 는 **신선하고 지수 갱신 시간일 때만**
    넘긴다(아니면 None — 선물 가격·프리미엄 조건만 남는다). 옵션 손절은 저장된 `premium_stop`
    으로만 발동한다(평균가·배수로 다시 계산하지 않는다)."""
    local = now.astimezone(KST)
    long_ = pos.direction == "LONG"
    if index is not None and pos.stop_pt is not None:
        if (long_ and index <= pos.stop_pt) or (not long_ and index >= pos.stop_pt):
            return "STOP", f"기초지수 {index} 손절선 {pos.stop_pt} 도달"
    price = quote.price if quote is not None and quote.price and quote.price > 0 else None
    if pos.kind == "FUTURES" and price is not None and pos.stop_fut is not None:
        if (long_ and price <= pos.stop_fut) or (not long_ and price >= pos.stop_fut):
            return "STOP", f"선물 {price} 손절가 {pos.stop_fut} 도달"
    if pos.kind != "FUTURES" and price is not None and pos.premium_stop is not None:
        if pos.vehicle == "OPTION_BUY" and price <= pos.premium_stop:
            return "STOP", f"프리미엄 {price} ≤ 보호선 {pos.premium_stop}"
        if pos.vehicle == "OPTION_SELL" and price >= pos.premium_stop:
            return "STOP", f"프리미엄 {price} ≥ 보호선 {pos.premium_stop}"
    ltd = date.fromisoformat(pos.last_trade_date) if pos.last_trade_date else None
    if ltd is not None:
        force = triggers.force_close_day_of(pos, cfg, last_trade_date=ltd, holiday_fn=holiday_fn)
        if today >= force and local.time() >= FORCE_CLOSE_AT:
            return "EXPIRY", f"만기 강제 청산(최종거래일 {ltd}, 청산일 {force})"
    if index is not None and pos.tp_pt is not None:
        if (long_ and index >= pos.tp_pt) or (not long_ and index <= pos.tp_pt):
            return "CLOSE", f"익절선 {pos.tp_pt} 도달(기초지수 {index})"
    if pos.horizon_until and today > date.fromisoformat(pos.horizon_until):
        return "CLOSE", f"보유기간 만료({pos.horizon_until})"
    if not cfg.hold_overnight:
        close = datetime.strptime(fo_close_hm(ltd == today), "%H:%M")
        cut = (close - timedelta(minutes=cfg.close_before_min)).time()
        if local.time() >= cut:
            return "SESSION_CLOSE", f"당일 청산(hold_overnight=false, {cut:%H:%M})"
    return None


# --- 유지 조건 ---------------------------------------------------------------------------


def _held_row(bal: FoBalance | None, pos: FoPositionRow):
    if bal is None:
        return None
    return next(
        (p for p in bal.positions if p.code == pos.code and p.contract_side == pos.contract_side),
        None,
    )


def _notional(pos: FoPositionRow, q: FoQuote | None, index: Decimal | None, mult: int) -> Decimal:
    """계약 1개 델타 명목 |δ| × 기초지수 × 승수. 모르면 0(검사 생략)."""
    base = index if index is not None else (q.underlying if q is not None else None)
    if base is None:
        return Decimal(0)
    if pos.kind == "FUTURES":
        delta = Decimal(1)
    elif q is not None and q.delta is not None:
        delta = abs(Decimal(str(q.delta)))
    else:
        return Decimal(0)
    return delta * base * Decimal(mult)


def maintain(
    ctx: FoExecContext,
    account: AccountConfig,
    held: list[FoPositionRow],
    *,
    bal: FoBalance,
    quotes: dict[int, FoQuote | None],
    index: Decimal | None,
    now: datetime,
    today: date,
) -> tuple[list[tuple[FoPositionRow, int, str]], list[str]]:
    """유지 조건 위반 → ([(포지션, 목표 수량, 사유)], 경고). 포지션마다 가장 작은 목표 하나."""
    cfg = ctx.settings.fo
    nav = bal.cash.nav
    if nav is None or nav <= 0 or not held:
        return [], []
    mult = cfg.product_cfg().multiplier
    targets: dict[int, tuple[int, list[str]]] = {}
    warnings: list[str] = []

    def want(pos: FoPositionRow, n: int, why: str) -> None:
        cur = targets.get(pos.id)
        n = max(n, 0)
        if cur is None or n < cur[0]:
            targets[pos.id] = (n, [*(cur[1] if cur else []), why])
        else:
            cur[1].append(why)

    ordered = sorted(held, key=lambda p: REDUCE_ORDER.index(p.vehicle))
    first = ordered[0]
    # 실제 노출 배수: 국면 cap 0·대조 0 은 신규 금지 사유이지 보유를 0 으로 만들지 않는다
    regime = ctx.regime_of()
    view = rules.view_state_of(
        ctx.view_of(), today=today, min_confidence=ctx.settings.hedge.agent_min_confidence
    )
    cap = cfg.regime_cap(regime)
    cap = cap if cap > 0 else cfg.max_leverage
    for pos in held:
        per = _notional(pos, quotes.get(pos.id), index, mult)
        if per <= 0:
            continue
        cf = rules.cross_factor(regime, view, cfg.cross_check, pos.direction, pos.vehicle,
                                sell_against_regime=cfg.option.sell_against_regime)  # fmt: skip
        lim = Decimal(str(min(cap, cfg.max_leverage) * (cf if cf > 0 else 1.0)))
        n_max = int((nav * lim) // per)
        if pos.qty_held > n_max:
            want(pos, n_max, f"노출 배수 {per * pos.qty_held / nav:.2f} > 한도 {lim:.2f}")
    if bal.cash.add_margin and bal.cash.add_margin > 0:
        want(first, first.qty_held - 1, f"추가증거금 {bal.cash.add_margin}")
    buf = nav * Decimal(str(cfg.margin_buffer_pct))
    if bal.cash.orderable_cash is not None and bal.cash.orderable_cash < buf:
        want(first, first.qty_held - 1, f"주문가능현금 {bal.cash.orderable_cash} < 버퍼 {buf}")
    for pos in held:  # 수명 예산(보유분 계획 손실 + 실현 손실 + 비용)
        row = _held_row(bal, pos)
        ref = pos.entry_premium if pos.kind != "FUTURES" else (row.avg_price if row else None)
        if ref is None or pos.nav_at_open is None:
            continue
        try:
            per_loss = sizing.planned_loss_per_contract(
                pos.vehicle, pos.kind, ref_price=ref, stop_fut=pos.stop_fut,
                premium_stop=pos.premium_stop, multiplier=mult,
            )  # fmt: skip
        except ValueError:
            continue
        b = sizing.lifetime_budget(pos, nav_at_open=pos.nav_at_open, r=cfg.risk_per_trade_pct,
                                   held_loss=per_loss * pos.qty_held,
                                   pending_loss=Decimal(0))  # fmt: skip
        if not b["ok"]:
            want(pos, pos.qty_held - 1, f"수명 예산 {b['used'] + b['planned']} > {b['limit']}")
    if cfg.hold_overnight:  # 스윙 유지 조건: 시나리오 최악 손실
        items = []
        for pos in held:
            q = quotes.get(pos.id)
            dl = executor._days_left(q, None, today)
            if q is None or dl is None:
                continue
            sign = 1 if pos.contract_side == "LONG" else -1
            items.append(sizing.StressItem(quote=q, signed_qty=sign * pos.qty_held, days_left=dl))
        if items:
            loss = sizing.stress_loss(items, cfg=cfg.stress, multiplier=mult)
            limit = nav * Decimal(str(cfg.overnight_risk_pct))
            if loss > limit:
                why = f"스트레스 최악 손실 {loss:.0f} > {limit:.0f}"
                if now.astimezone(KST).time() >= STRESS_CONFIRM_AT:
                    want(first, first.qty_held - 1, why)
                else:
                    warnings.append(why)
    out = [(p, targets[p.id][0], "; ".join(targets[p.id][1])) for p in ordered if p.id in targets]
    return out, warnings


# --- 틱 ----------------------------------------------------------------------------------


def _index(ctx: FoExecContext, now: datetime) -> tuple[Decimal | None, bool]:
    """(지수, 보호선에 써도 되는가) — 지수 갱신 시간 ∧ 시장 시각 `max_quote_age_s` 안."""
    mc = ctx.market_client
    if mc is None:
        return None, False
    try:
        idx, asof = mc.get_kospi200()
    except Exception as e:  # noqa: BLE001 — 지수가 없으면 선물 가격 조건만
        log.warning("코스피200 조회 실패(선물 가격 조건만): %s", e)
        return None, False
    fresh = (
        idx is not None
        and asof is not None
        and index_live(now)
        and (now - asof).total_seconds() <= ctx.settings.fo.max_quote_age_s
    )
    return idx, fresh


def _discard_pending_triggers(ctx: FoExecContext, pos: FoPositionRow, why: str, now: datetime):
    rows = ctx.conn.execute(
        "SELECT id FROM triggers WHERE market = 'KR' AND ticker = ? AND kind LIKE 'fo:%' "
        "AND handled_at IS NULL",
        (pos.code,),
    ).fetchall()
    for r in rows:
        repo.mark_trigger_handled(ctx.conn, r["id"], f"폐기: {why}"[:200],
                                  handled_at=now.isoformat())  # fmt: skip


def _decision_ref(conn: sqlite3.Connection, pos: FoPositionRow) -> dict[str, Any]:
    """이 포지션의 최근 결정(재평가 라운드 우선, 없으면 그날 아침 결정)의 기준 지수·무효화 조건."""
    row = conn.execute(
        "SELECT index_at_decision, data FROM fo_decisions WHERE position_id = ? "
        "ORDER BY round DESC, id DESC LIMIT 1",
        (pos.id,),
    ).fetchone()
    if row is None and pos.decision_date:
        row = conn.execute(
            "SELECT index_at_decision, data FROM fo_decisions WHERE date = ? AND "
            "position_id IS NULL AND round = 0 ORDER BY id DESC LIMIT 1",
            (pos.decision_date,),
        ).fetchone()
    if row is None:
        return {}
    try:
        data = json.loads(row["data"]) if row["data"] else {}
    except ValueError:
        data = {}
    dec = data.get("decision") or {}
    idx = row["index_at_decision"]
    return {
        "ref_index": Decimal(str(idx)) if idx not in (None, "") else None,
        "invalidation": list(dec.get("invalidation_conditions") or []),
        "horizon_days": dec.get("horizon_days"),
    }


def _novel_news(conn: sqlite3.Connection, now: datetime, minutes: int) -> int:
    since = (now - timedelta(minutes=minutes)).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) FROM news_items WHERE market = 'KR' AND novel = 1 AND received_at >= ?",
        (since,),
    ).fetchone()
    return int(row[0]) if row else 0


def _settle(ctx: FoExecContext, account: AccountConfig, pos: FoPositionRow, bal: FoBalance | None,
            today: date) -> dict[str, Any] | None:  # fmt: skip
    """정산 대기 → 최종거래일 다음 운영일 이후 잔고에 없으면 `settled`(정산 손익은 체결 동기화가
    적은 실현 손실 기준). 잔고에 남아 있으면 그대로 두고 운영 점검이 지연을 알린다."""
    if bal is None or not pos.last_trade_date or today <= date.fromisoformat(pos.last_trade_date):
        return None
    if _held_row(bal, pos) is not None:
        return None
    with store.tx(ctx.conn):
        store.transition(ctx.conn, pos.id, expected_version=pos.version, status="settled",
                         closed_at=now_iso(), note="정산 확인(잔고 없음)")  # fmt: skip
    executor._notify(ctx, messages.fo_settlement(account=account.id, code=pos.code,
                                                 name=executor._name_of(ctx, pos.code),
                                                 pnl=-pos.realized_loss))  # fmt: skip
    return {"position_id": pos.id, "status": "settled"}


def _recall_all(ctx: FoExecContext, account: AccountConfig, reason: str) -> list[dict]:
    out = []
    for pos in store.open_positions(ctx.conn, account.id):
        if any(is_entry(x.intent) for x in store.live_legs(ctx.conn, position_id=pos.id)):
            rr = executor.recall_entries(ctx, account, pos.id, reason=reason)
            out.append({"position_id": pos.id, "reason": reason, **rr.__dict__})
    return out


def tick(
    ctx: FoExecContext,
    *,
    request_reeval: RequestFn | None = None,
    holiday_fn: Callable[[date], bool] | None = None,
) -> dict[str, Any]:
    now, today = executor._today(ctx)
    index, index_fresh = _index(ctx, now)
    out: dict[str, Any] = {"date": today.isoformat(), "index": str(index) if index else None,
                           "index_fresh": index_fresh, "accounts": {}}  # fmt: skip
    for a in ctx.accounts:
        if a.asset != "fo":
            continue
        try:
            out["accounts"][a.id] = _tick_account(
                ctx, a, now=now, today=today, index=index if index_fresh else None,
                raw_index=index, request_reeval=request_reeval or _enqueue,
                holiday_fn=holiday_fn,
            )  # fmt: skip
        except Exception as e:  # noqa: BLE001 — 계좌 하나의 실패가 다른 계좌 감시를 막지 않는다
            log.exception("파생 모니터 %s 실패: %s", a.id, e)
            out["accounts"][a.id] = {"error": f"{type(e).__name__}: {e}"}
    return out


def _tick_account(
    ctx: FoExecContext,
    a: AccountConfig,
    *,
    now: datetime,
    today: date,
    index: Decimal | None,
    raw_index: Decimal | None,
    request_reeval: RequestFn,
    holiday_fn: Callable[[date], bool] | None,
) -> dict[str, Any]:
    conn, cfg = ctx.conn, ctx.settings.fo
    res: dict[str, Any] = {"recovered": None, "exits": [], "reduced": [], "maintain": [],
                           "halted": False, "recalled": [], "triggers": [], "entries": [],
                           "snapshot": False, "errors": []}  # fmt: skip
    broker = ctx.brokers.get(a.id)
    bal: FoBalance | None = None
    if broker is None:
        res["errors"].append("broker 없음")
    else:
        try:
            bal = broker.get_balance()
        except Exception as e:  # noqa: BLE001 — 잔고 실패: 신규 보류, 축소는 원장 잔량으로
            res["errors"].append(f"balance: {type(e).__name__}: {e}")
    # ① 복구·동기화
    if bal is not None and broker is not None:
        rep = positions.recover(
            conn, account_id=a.id, balance=bal, order_state_of=broker.get_order_state,
            fills_of=broker.get_fills, today=today, now=now,
        )  # fmt: skip
        conn.commit()
        res["recovered"] = {"unknown": rep.unknown, "settling": rep.settling,
                            "legs_fixed": rep.legs_fixed, "deferred": rep.deferred}  # fmt: skip
        for msg in rep.alerts:
            _notify_once(ctx, a.id, f"recover:{hash(msg)}", msg, today)
    for pos in store.open_positions(conn, a.id):
        if pos.status in ("closing", "resizing", "exit_failed") or pos.active_reduce_leg_id:
            res["reduced"].append(executor.continue_reduce(ctx, a, pos))
        elif pos.status == "entering" and not store.live_legs(conn, position_id=pos.id):
            executor.finalize(ctx, pos)
        elif pos.status == "awaiting_settlement":
            s = _settle(ctx, a, pos, bal, today)
            if s:
                res["reduced"].append(s)
    # ② 보호선
    quotes: dict[int, FoQuote | None] = {}
    for pos in store.open_positions(conn, a.id):
        if pos.status not in ("held", "entering") or pos.qty_held <= 0:
            continue
        if pos.active_reduce_leg_id:
            continue
        q = executor.quote_of(ctx, pos.code, pos.kind)
        quotes[pos.id] = q
        hit = exit_reason(pos, cfg=cfg, index=index, quote=q, now=now, today=today,
                          holiday_fn=holiday_fn)  # fmt: skip
        if hit is None:
            continue
        intent, why = hit
        _discard_pending_triggers(ctx, pos, why, now)
        _notify_once(ctx, a.id, f"exit:{pos.id}:{intent}",
                     messages.fo_exit(account=a.id, code=pos.code,
                                      name=executor._name_of(ctx, pos.code), intent=intent,
                                      reason=why, qty=pos.qty_held), today)  # fmt: skip
        r = executor.reduce(ctx, a, pos, intent=intent, qty=None, reason=why)
        res["exits"].append({"position_id": pos.id, "intent": intent, "reason": why, **r})
    # ③ 유지 조건
    held = [p for p in store.open_positions(conn, a.id)
            if p.status == "held" and p.qty_held > 0 and not p.active_reduce_leg_id]  # fmt: skip
    if bal is not None and held:
        for p in held:
            if p.id not in quotes:
                quotes[p.id] = executor.quote_of(ctx, p.code, p.kind)
        acts, warns = maintain(ctx, a, held, bal=bal, quotes=quotes, index=raw_index, now=now,
                               today=today)  # fmt: skip
        if warns:
            _notify_once(ctx, a.id, "maintain-warning",
                         messages.fo_maintain_warning(account=a.id, reasons=warns), today)
        for pos, target, why in acts:
            res["maintain"].append(_risk_reduce(ctx, a, pos, target, why, today, now))
    # ④ 일일 손실·진입 회수
    halted_key = executor.halted_key(a.id)
    if bal is not None and bal.cash.nav:
        pnl = bal.cash.realized_today + bal.cash.unrealized
        limit = bal.cash.nav * Decimal(str(cfg.daily_loss_stop_pct))
        if pnl <= -limit and control.get_flag(conn, halted_key) != today.isoformat():
            control.set_flag(conn, halted_key, today.isoformat())
            executor._notify(ctx, messages.fo_halted(account=a.id, loss=pnl, limit=limit))
    halted = control.get_flag(conn, halted_key) == today.isoformat()
    res["halted"] = halted
    recall_why = _recall_reason(ctx, a, now=now, halted=halted)
    if recall_why:
        res["recalled"] = _recall_all(ctx, a, recall_why)
    # ⑤ 트리거
    if not control.is_paused(conn):
        for pos in store.open_positions(conn, a.id):
            if pos.status != "held" or pos.qty_held <= 0 or pos.active_reduce_leg_id:
                continue
            t = _trigger_for(ctx, pos, quotes.get(pos.id), index=raw_index, now=now,
                             today=today, holiday_fn=holiday_fn)  # fmt: skip
            if t is None:
                continue
            tid = triggers.record(conn, pos, t, now=now)
            rq = reeval.request(ctx, account_id=a.id, position_id=pos.id, trigger_id=tid,
                                trigger={"kind": triggers.ledger_kind(t.kind),
                                         "detail": t.detail}, today=today)  # fmt: skip
            if rq.get("ok"):
                request_reeval(ctx, rq["params"])
            else:
                repo.mark_trigger_handled(conn, tid, f"실패: {rq.get('error')}"[:200],
                                          handled_at=now.isoformat())  # fmt: skip
            res["triggers"].append({"position_id": pos.id, "kind": t.kind, "trigger_id": tid,
                                    "request": rq.get("params")})  # fmt: skip
    # ⑥ 보류 진입·REPLACE
    res["entries"] = _pending_entries(ctx, a, now=now, today=today, blocked=recall_why)
    res["entries"] += reeval.continue_replace(ctx, a)
    # ⑦ 스냅샷
    if broker is not None:
        try:
            fo_balance.refresh(conn, broker, a, now=ctx.now, cfg=cfg)
            res["snapshot"] = True
        except Exception as e:  # noqa: BLE001
            res["errors"].append(f"snapshot: {type(e).__name__}: {e}")
    return res


def _risk_reduce(ctx: FoExecContext, a: AccountConfig, pos: FoPositionRow, target: int, why: str,
                 today: date, now: datetime) -> dict[str, Any]:  # fmt: skip
    _notify_once(ctx, a.id, f"maintain:{pos.id}:{target}",
                 messages.fo_risk_reduce(account=a.id, code=pos.code,
                                         name=executor._name_of(ctx, pos.code), reason=why,
                                         held=pos.qty_held, target=target), today)  # fmt: skip
    _discard_pending_triggers(ctx, pos, f"위험 축소: {why}", now)
    if target <= 0:
        r = executor.reduce(ctx, a, pos, intent="RISK_REDUCE", qty=None, reason=why)
    else:
        with store.tx(ctx.conn):
            ok = store.transition(ctx.conn, pos.id, expected_version=pos.version,
                                  qty_target=target)  # fmt: skip
        if not ok:
            return {"position_id": pos.id, "status": "skipped", "note": "버전 경합"}
        cur = store.get_position(ctx.conn, pos.id)
        assert cur is not None
        r = executor.reduce(ctx, a, cur, intent="RESIZE_DOWN", qty=None, reason=why)
    return {"position_id": pos.id, "target": target, "reason": why, **r}


def _recall_reason(ctx: FoExecContext, a: AccountConfig, *, now: datetime,
                   halted: bool) -> str | None:  # fmt: skip
    """살아 있는 진입 다리를 회수해야 하는 사유(없으면 None)."""
    conn, cfg = ctx.conn, ctx.settings.fo
    if halted:
        return "일일 손실 한도"
    if not cfg.enabled:
        return "fo.enabled=false"
    ks = killswitch.reason(conn, a.id)
    if ks:
        return f"킬스위치: {ks}"
    if now.astimezone(KST).strftime("%H:%M") >= cfg.entry_window.cutoff:
        return f"진입 cutoff({cfg.entry_window.cutoff}) 지남"
    for pos in store.open_positions(conn, a.id):
        key = executor.verified_key(ctx.settings, a, pos.vehicle)
        if control.get_flag(conn, key) != CONTRACT_VERSION and any(
            is_entry(x.intent) for x in store.live_legs(conn, position_id=pos.id)
        ):
            return f"{pos.vehicle} 검증 플래그 없음"
    return None


def _trigger_for(ctx: FoExecContext, pos: FoPositionRow, q: FoQuote | None, *,
                 index: Decimal | None, now: datetime, today: date,
                 holiday_fn: Callable[[date], bool] | None) -> triggers.Trigger | None:  # fmt: skip
    cfg = ctx.settings.fo
    ref = _decision_ref(ctx.conn, pos)
    days = ref.get("horizon_days")
    if days is None and pos.horizon_until and pos.decision_date:
        days = (date.fromisoformat(pos.horizon_until) - date.fromisoformat(pos.decision_date)).days
    profile = horizon.profile_of(ctx.settings.risk, days)
    iv = q.iv if q is not None else None
    iv_key = f"fo_ref_iv:{pos.id}"
    ref_iv = control.get_flag(ctx.conn, iv_key)
    if ref_iv is None and iv is not None and pos.kind != "FUTURES":
        control.set_flag(ctx.conn, iv_key, iv)
        ref_iv = iv
    opinion = None
    if pos.last_trade_date:
        force = triggers.force_close_day_of(
            pos, cfg, last_trade_date=date.fromisoformat(pos.last_trade_date),
            holiday_fn=holiday_fn,
        )  # fmt: skip
        opinion = triggers.opinion_day_of(force, holiday_fn)
    underlying = index if index is not None else (q.underlying if q is not None else None)
    return triggers.check(
        ctx.conn, position=pos, underlying=underlying, iv=iv, cfg=cfg, profile=profile, now=now,
        today=today, novel_news=_novel_news(ctx.conn, now, cfg.reeval.interval_min),
        opinion_day=opinion, ref_index=ref.get("ref_index"), ref_iv=ref_iv,
        take_profit_pt=pos.tp_pt, invalidation_conditions=ref.get("invalidation"),
    )  # fmt: skip


def _pending_entries(ctx: FoExecContext, a: AccountConfig, *, now: datetime, today: date,
                     blocked: str | None) -> list[dict[str, Any]]:  # fmt: skip
    """아침 보류 진입(진입 창 밖 결정)을 진입 창이 열리면 실행 직전 재계산으로 낸다. 회수 사유가
    있거나 cutoff 가 지났으면 버린다. 플래그는 실행 **전에** 지운다(중복 실행 방지)."""
    from trader.orchestrator import tools

    conn, cfg = ctx.conn, ctx.settings.fo
    flag = tools.fo_pending_entry(conn, a.id, today)
    if flag is None:
        return []
    key = f"{tools.FO_PENDING_FLAG}{a.id}"
    if blocked:
        control.set_flag(conn, key, None)
        return [{"discarded": blocked, "request_id": flag.get("request_id")}]
    if not rules._in_entry_window(now, cfg):
        return []  # 08:45~09:00 은 기다린다(cutoff 뒤는 blocked 로 이미 버렸다)
    control.set_flag(conn, key, None)
    try:
        env = FoDecisionEnvelope.model_validate(flag["envelope"])
    except Exception as e:  # noqa: BLE001
        return [{"discarded": f"봉투 검증 실패: {e}"}]
    res = executor.run_leg(ctx, a, env, round=0, source="pending")
    return [{"pending": flag.get("request_id"), "results": res}]
