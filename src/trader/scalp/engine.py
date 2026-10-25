"""분 단위 단타 집행 (Phase 23 S) — **모의 계좌 전용**, 기본 끔.

틱(60초)마다: ① 허용 판정(설정·paused·bear 국면·전략가 RISK_OFF/scalp_ok=false·일일 손실 한도) →
② 후보(오늘 `_scan` picks + 새 뉴스 재분석 종목 + 관심종목 − 보유·헤지·오늘 이미 거래) ≤
`max_universe` → ③ 보유 중 단타는 청산 규칙, 나머지는 1분봉 진입 규칙(`signals.py`) →
④ `executor.execute(scalp_leg=...)` 로 계좌 모드·dry_run·게이트를 그대로 지키며 주문(실전 계좌는
`skip_accounts`). 상태는 `state/scalp/<date>/<market>.json`, 사건은 같은 폴더 `events.jsonl`.
결정 행(`decisions`)은 만들지 않는다 — 오늘 LLM 결정과 같은 (date, market, ticker) 키를 덮기 때문.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trader.agents.schemas import Decision
from trader.broker.errors import KisApiError
from trader.config import MARKET_TZ, AccountConfig, ScalpCfg, Settings
from trader.control import control
from trader.notify.telegram import Notifier, NullNotifier
from trader.orchestrator import executor, run_store, tools
from trader.orchestrator.run_store import read_checkpoint
from trader.platform import paths
from trader.risk import regime as regime_mod
from trader.scalp import signals

logger = logging.getLogger("trader.scalp")

MINUTES_NEEDED = 60  # 틱마다 받는 1분봉 수 (진입 판정 25봉 + 여유)


@dataclass
class ScalpContext:
    settings: Settings
    accounts: list[AccountConfig]
    conn: sqlite3.Connection
    brokers: dict[str, Any]
    market_client: Any
    open_hm: str  # 그 시장 정규장 시작 "HH:MM" (현지)
    close_hm: str
    notifier: Notifier = field(default_factory=NullNotifier)
    now: Any = field(default=lambda: datetime.now(UTC))
    state: Path | None = None
    dry_run: bool = False
    exchanges: dict[str, str] = field(default_factory=dict)  # US ticker → NAS|NYS|AMS


# --- 상태 파일 -------------------------------------------------------------------------


def state_dir(state: Path | None, today: date) -> Path:
    return (state or paths.state_dir()) / "scalp" / today.isoformat()


def load_state(state: Path | None, today: date, market: str) -> dict:
    p = state_dir(state, today) / f"{market}.json"
    if not p.exists():
        return {"open": [], "closed": [], "done": [], "halted": None}
    return json.loads(p.read_text(encoding="utf-8"))


def save_state(state: Path | None, today: date, market: str, data: dict) -> None:
    d = state_dir(state, today)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{market}.json.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(d / f"{market}.json")


def _event(state: Path | None, today: date, market: str, kind: str, **kw: Any) -> None:
    d = state_dir(state, today)
    d.mkdir(parents=True, exist_ok=True)
    row = {"at": datetime.now(UTC).isoformat(), "market": market, "kind": kind} | kw
    with (d / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


# --- 허용 판정 -------------------------------------------------------------------------


def allowed(ctx: ScalpContext, market: str, today: date, st: dict) -> str | None:
    """단타를 돌리지 않을 이유(문자열) 또는 None."""
    cfg = ctx.settings.scalp
    if not cfg.enabled:
        return "scalp.enabled=false"
    if not any(a.env == "paper" and market in a.markets for a in ctx.accounts):
        return "모의 계좌 없음 (단타는 모의 전용)"
    if control.is_paused(ctx.conn):
        return "일시정지"
    if st.get("halted"):
        return f"일일 손실 한도: {st['halted']}"
    if ctx.settings.hedge.enabled:
        reg = (regime_mod.load(ctx.conn, market) or {}).get("state")
        if reg == "bear":
            return "하락 국면(bear)"
        view = tools.market_view_today(ctx.conn, market, today)
        if view and float(view.get("confidence") or 0) >= ctx.settings.hedge.agent_min_confidence:
            if view.get("scalp_ok") is False:
                return "전략가 제안: 단타 금지(scalp_ok=false)"
            if view.get("stance") == "RISK_OFF":
                return "전략가 제안 RISK_OFF"
    return None


# --- 후보 -------------------------------------------------------------------------------


def candidates(ctx: ScalpContext, market: str, today: date, st: dict) -> list[dict]:
    """오늘 picks + 새 뉴스 재분석 종목 + 관심종목 − 보유·헤지·오늘 거래분. {ticker, exchange}."""
    cfg = ctx.settings.scalp
    mdir = run_store.market_dir(today, market, ctx.state)
    scan = read_checkpoint(mdir, "_scan") or {}
    out: list[dict] = []
    seen: set[str] = set()

    def add(t: str | None, ex: str | None = None) -> None:
        if not t or t in seen:
            return
        seen.add(t)
        out.append({"ticker": t, "exchange": ex or ctx.exchanges.get(t)})

    for p in scan.get("picks") or []:
        add(p.get("ticker"))
    for t in scan.get("analyze_targets") or []:
        if t.get("source") == "pick":
            add(t.get("ticker"), t.get("exchange"))
    lo, hi = _day_bounds(today, market)
    for r in ctx.conn.execute(
        "SELECT params FROM job_requests WHERE source='news' AND kind='analyze_ticker' "
        "AND requested_at >= ? AND requested_at < ?",
        (lo, hi),
    ).fetchall():
        params = json.loads(r["params"]) if r["params"] else {}
        if params.get("market") == market:
            add(params.get("ticker"), params.get("exchange"))
    for w in control.get_watchlist(ctx.conn, market):
        add(w)
    held: set[str] = set()
    for a in ctx.accounts:
        b = ctx.brokers.get(a.id)
        if b is None or market not in a.markets:
            continue
        try:
            held |= {p.ticker for p in b.get_positions(market)}
        except (KisApiError, ValueError) as e:
            logger.warning("단타 보유 조회 실패 %s: %s", a.id, e)
    hedge = set(ctx.settings.hedge.tickers(market)) if ctx.settings.hedge.enabled else set()
    open_t = {o["ticker"] for o in st.get("open") or []}
    done = set(st.get("done") or [])
    picked = [
        c
        for c in out
        if c["ticker"] not in held - open_t
        and c["ticker"] not in hedge
        and c["ticker"] not in done
        and c["ticker"] not in open_t
    ]
    if market == "US":
        picked = [c for c in picked if c.get("exchange")]
    return picked[: cfg.max_universe]


def _day_bounds(today: date, market: str) -> tuple[str, str]:
    from trader.ledger import repo

    return repo.day_bounds(today, market)


# --- 틱 ---------------------------------------------------------------------------------


def _local_hms(ctx: ScalpContext, market: str) -> str:
    return ctx.now().astimezone(MARKET_TZ[market]).strftime("%H%M%S")


def _minutes(ctx: ScalpContext, market: str, c: dict) -> list:
    if market == "KR":
        return ctx.market_client.get_minute_kr(c["ticker"], count=MINUTES_NEEDED)
    return ctx.market_client.get_minute_us(c["ticker"], c["exchange"], count=MINUTES_NEEDED)


def _exec_ctx(ctx: ScalpContext) -> executor.ExecContext:
    return executor.ExecContext(
        settings=ctx.settings,
        accounts=ctx.accounts,
        conn=ctx.conn,
        brokers=ctx.brokers,
        notifier=ctx.notifier,
        state=ctx.state,
        now=ctx.now,
    )


def _skip_accounts(ctx: ScalpContext, market: str) -> frozenset[str]:
    return frozenset(a.id for a in ctx.accounts if a.env != "paper" or market not in a.markets)


def _decision(action: str, ticker: str, market: str, price: Decimal, cfg: ScalpCfg, why: str):
    return Decision(
        ticker=ticker,
        action=action,  # type: ignore[arg-type]
        target_weight=cfg.max_weight_pct if action == "BUY" else 0.0,
        order_type="LIMIT" if action == "BUY" or market == "US" else "MARKET",
        limit_price=float(price) if (action == "BUY" or market == "US") else None,
        stop_loss=None,
        take_profit=None,
        horizon_days=1,
        confidence=1.0,
        rationale=f"분 단위 단타({'진입' if action == 'BUY' else '청산'}): {why}"[:500],
        invalidation_conditions=[],
    )


def _filled_qty(results: list[dict]) -> dict[str, int]:
    """계좌별 접수(또는 dry_run 승인) 수량."""
    out: dict[str, int] = {}
    for r in results:
        if r.get("error") or not r.get("approved"):
            continue
        order = r.get("order")
        if order is None:
            if r.get("dry_run"):
                out[r["account_id"]] = int(r.get("qty") or 0)
            continue
        if order.get("status") in ("accepted", "unknown"):
            out[r["account_id"]] = int(r.get("qty") or 0)
    return {k: v for k, v in out.items() if v > 0}


def tick(ctx: ScalpContext, *, market: str, today: date) -> dict:
    """1틱: 청산 → 진입. 반환 {entries, exits, skipped?, candidates}."""
    cfg = ctx.settings.scalp
    st = load_state(ctx.state, today, market)
    why = allowed(ctx, market, today, st)
    if why:
        return {"skipped": why}
    ectx = _exec_ctx(ctx)
    skip = _skip_accounts(ctx, market)
    now_t = _local_hms(ctx, market)
    out: dict[str, Any] = {"entries": [], "exits": [], "candidates": 0}
    # --- 청산 ---
    still_open = []
    for pos in st.get("open") or []:
        try:
            q = (
                ctx.market_client.get_price_kr(pos["ticker"]).price
                if market == "KR"
                else ctx.market_client.get_price_us(pos["ticker"], pos.get("exchange")).price
            )
        except (KisApiError, ValueError) as e:
            logger.warning("단타 시세 실패 %s: %s", pos["ticker"], e)
            still_open.append(pos)
            continue
        reason = signals.exit_reason(
            entry_price=Decimal(str(pos["entry_price"])),
            entry_time=pos["entry_time"],
            price=q,
            now_time=now_t,
            cfg=cfg,
            close_hm=ctx.close_hm,
        )
        if reason is None:
            still_open.append(pos)
            continue
        dec = _decision("SELL", pos["ticker"], market, q, cfg, reason)
        results = executor.execute(
            ectx,
            dec,
            date=today,
            market=market,
            price=q,
            exchange=pos.get("exchange"),
            dry_run=ctx.dry_run,
            decision_id=None,
            skip_accounts=skip,
            scalp_leg="exit",
        )
        ret = float(q / Decimal(str(pos["entry_price"])) - 1)
        closed = pos | {
            "exit_price": str(q),
            "exit_time": now_t,
            "reason": reason,
            "ret": round(ret, 5),
            "exit_results": [
                {k: r.get(k) for k in ("account_id", "approved", "qty", "reasons")} for r in results
            ],
        }
        st.setdefault("closed", []).append(closed)
        out["exits"].append({"ticker": pos["ticker"], "reason": reason, "ret": round(ret, 5)})
        _event(ctx.state, today, market, "exit", **closed)
        ctx.notifier.send(
            f"[단타 청산] {market} {pos['ticker']} {reason} {ret:+.2%} @ {q}"
            + (" (dry-run)" if ctx.dry_run else "")
        )
    st["open"] = still_open
    # 일일 손실 한도: 청산된 거래의 (수익률 × 비중) 합
    realized = sum(float(c.get("ret") or 0) for c in st.get("closed") or []) * cfg.max_weight_pct
    if realized <= -cfg.daily_stop_pct and not st.get("halted"):
        st["halted"] = f"실현 {realized:+.2%} ≤ -{cfg.daily_stop_pct:.2%}"
        ctx.notifier.send(f"[단타 중단] {market} {st['halted']} — 오늘 신규 진입 없음")
        _event(ctx.state, today, market, "halt", realized=realized)
    # --- 진입 ---
    if not st.get("halted") and len(st["open"]) < cfg.max_positions:
        cands = candidates(ctx, market, today, st)
        out["candidates"] = len(cands)
        for c in cands:
            if len(st["open"]) >= cfg.max_positions:
                break
            try:
                bars = _minutes(ctx, market, c)
            except (KisApiError, ValueError) as e:
                logger.warning("단타 분봉 실패 %s: %s", c["ticker"], e)
                continue
            e = signals.entry_signal(bars, cfg, open_hm=ctx.open_hm, close_hm=ctx.close_hm)
            if e is None:
                continue
            dec = _decision("BUY", c["ticker"], market, e.price, cfg, e.reason())
            results = executor.execute(
                ectx,
                dec,
                date=today,
                market=market,
                price=e.price,
                exchange=c.get("exchange"),
                dry_run=ctx.dry_run,
                decision_id=None,
                skip_accounts=skip,
                scalp_leg="entry",
            )
            st.setdefault("done", []).append(c["ticker"])  # 하루 종목당 1회(거부돼도)
            qty = _filled_qty(results)
            if not qty:
                reasons = [r.get("reasons") for r in results]
                _event(
                    ctx.state, today, market, "entry_rejected", ticker=c["ticker"], reasons=reasons
                )
                continue
            pos = {
                "ticker": c["ticker"],
                "exchange": c.get("exchange"),
                "entry_price": str(e.price),
                "entry_time": e.time,
                "entry_at": ctx.now().isoformat(),
                "qty": qty,
                "reason": e.reason(),
                "dry_run": ctx.dry_run,
            }
            st["open"].append(pos)
            out["entries"].append({"ticker": c["ticker"], "price": str(e.price), "qty": qty})
            _event(ctx.state, today, market, "entry", **pos)
            ctx.notifier.send(
                f"[단타 진입] {market} {c['ticker']} @ {e.price} {e.reason()}"
                + (" (dry-run)" if ctx.dry_run else "")
            )
    save_state(ctx.state, today, market, st)
    return out


def report(state: Path | None, *, market: str, days: int = 5, today: date | None = None) -> dict:
    """최근 운영일의 단타 상태 파일을 모아 승률·사유별 집계."""
    from datetime import timedelta

    from trader.risk import costs

    today = today or datetime.now(UTC).date()
    trades: list[dict] = []
    per_day = []
    for i in range(days * 2):
        d = today - timedelta(days=i)
        p = state_dir(state, d) / f"{market}.json"
        if not p.exists():
            continue
        st = json.loads(p.read_text(encoding="utf-8"))
        closed = st.get("closed") or []
        trades += closed
        per_day.append({"date": d.isoformat(), "trades": len(closed), "halted": st.get("halted")})
        if len(per_day) >= days:
            break
    summary = signals.summarize(trades, round_trip_bp=costs.round_trip_bp(market))
    return {"market": market, "days": per_day, "summary": summary}
