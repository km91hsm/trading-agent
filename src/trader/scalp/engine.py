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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from trader import names
from trader.agents import schemas
from trader.agents.schemas import Decision
from trader.broker.errors import KisApiError
from trader.config import MARKET_TZ, AccountConfig, ScalpCfg, Settings
from trader.control import control
from trader.notify import messages
from trader.notify.telegram import Notifier, NullNotifier
from trader.orchestrator import executor, run_store, tools
from trader.orchestrator.run_store import read_checkpoint
from trader.platform import paths
from trader.risk import regime as regime_mod
from trader.scalp import signals

logger = logging.getLogger("trader.scalp")

MINUTES_NEEDED = 60  # 틱마다 받는 1분봉 수 (진입 판정 25봉 + 여유)


def _label(ctx: ScalpContext, market: str, ticker: str) -> str:
    """알림용 '종목명(코드)' (원장·캐시만)."""
    return names.display(ctx.conn, (ctx.state or paths.state_dir()) / "cache", market, ticker)


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
    judge: ScalpJudge | None = None  # Phase 25 판정자(scalp.judge 일 때 데몬이 넣는다)


class ScalpJudge:
    """분 단위 LLM 판정을 틱 밖 스레드에서 돌린다(Phase 25). 틱은 submit 만 하고 다음 틱에 poll —
    판정을 기다리느라 손절·청산 점검이 밀리지 않는다. 데몬 재시작으로 future 가 사라지면 poll 은
    None(= 판정 없음 → 진입 안 함). ask(payload, schema) 는 `quick.ask_json` 을 감싼 함수."""

    def __init__(self, ask: Callable[[dict, type], Any], *, max_workers: int = 2) -> None:
        from concurrent.futures import ThreadPoolExecutor

        self._ask = ask
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="scalp-judge")
        self._futs: dict[str, Any] = {}

    def submit(self, key: str, payload: dict, schema: type) -> None:
        f = self._futs.get(key)
        if f is not None and not f.done():
            return
        self._futs[key] = self._pool.submit(self._ask, payload, schema)

    def pending(self, key: str) -> bool:
        f = self._futs.get(key)
        return f is not None and not f.done()

    def poll(self, key: str) -> Any:
        """판정 결과 | "pending" | None(제출 기록 없음·실패·상한)."""
        f = self._futs.get(key)
        if f is None:
            return None
        if not f.done():
            return "pending"
        self._futs.pop(key, None)
        try:
            return f.result()
        except Exception as e:  # noqa: BLE001 - 판정 실패는 '판정 없음'
            logger.warning("단타 판정 실패 %s: %s", key, e)
            return None

    def discard(self, key: str) -> None:
        """포지션이 닫히면 그 포지션의 남은 조언을 버린다(다음 포지션이 옛 결과를 읽지 않게)."""
        f = self._futs.pop(key, None)
        if f is not None:
            f.cancel()

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def entry_key(market: str, today: date, ticker: str) -> str:
    return f"entry:{market}:{today.isoformat()}:{ticker}"


def advice_key(market: str, pos: dict) -> str:
    """조언 키는 포지션 단위(시장·종목·진입 시각) — 같은 종목의 다른 날 포지션과 섞이지 않는다."""
    return f"hold:{market}:{pos['ticker']}:{pos.get('entry_at') or pos.get('entry_time')}"


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
    done = set(st.get("done") or []) | set(st.get("pending") or {})
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


def _decision(
    action: str,
    ticker: str,
    market: str,
    price: Decimal,
    cfg: ScalpCfg,
    why: str,
    *,
    weight_mult: float = 1.0,
):
    return Decision(
        ticker=ticker,
        action=action,  # type: ignore[arg-type]
        target_weight=cfg.max_weight_pct * weight_mult if action == "BUY" else 0.0,
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


def _quote(ctx: ScalpContext, market: str, ticker: str, exchange: str | None) -> Decimal:
    return (
        ctx.market_client.get_price_kr(ticker).price
        if market == "KR"
        else ctx.market_client.get_price_us(ticker, exchange).price
    )


def _bars_json(bars: list, n: int = 20) -> list[dict]:
    return [
        {
            "time": c.time,
            "open": str(c.open),
            "high": str(c.high),
            "low": str(c.low),
            "close": str(c.close),
            "volume": c.volume,
        }
        for c in bars[-n:]
    ]


def _judge_context(ctx: ScalpContext, market: str, ticker: str, exchange: str | None) -> dict:
    """판정 입력의 공통 부분: 호가 잔량·오늘 뉴스·국면·전략가 제안 (실패한 절은 None/[])."""
    from trader.sources import news_store

    book = None
    try:
        ob = (
            ctx.market_client.get_orderbook_kr(ticker)
            if market == "KR"
            else ctx.market_client.get_orderbook_us(ticker, exchange)
        )
        book = {
            "asks": [[str(p), q] for p, q in ob.asks[:5]],
            "bids": [[str(p), q] for p, q in ob.bids[:5]],
        }
    except Exception as e:  # noqa: BLE001 - 호가는 보조 입력
        logger.debug("단타 호가 실패 %s: %s", ticker, e)
    try:
        news = [
            {
                "title": n.get("title"),
                "source": n.get("source"),
                "published_at": n.get("published_at"),
            }
            for n in news_store.recent(ctx.conn, market, asof=ctx.now(), hours=8, ticker=ticker)[
                :10
            ]
        ]
    except Exception:  # noqa: BLE001
        news = []
    reg = regime_mod.load(ctx.conn, market) or {}
    view = tools.market_view_today(ctx.conn, market, ctx.now().astimezone(MARKET_TZ[market]).date())
    return {
        "orderbook": book,
        "news": news,
        "regime": {"state": reg.get("state"), "count": reg.get("count")},
        "market_view": (
            {k: view.get(k) for k in ("stance", "confidence", "scalp_ok")} if view else None
        ),
    }


def _limits(cfg: ScalpCfg) -> dict:
    return {
        "take_profit_pct": cfg.take_profit_pct,
        "stop_pct": cfg.stop_pct,
        "max_hold_min": cfg.max_hold_min,
    }


def clamp_verdict(v: Any, cfg: ScalpCfg) -> dict:
    """판정자의 청산 파라미터를 설정값 안으로: 손절은 넓힐 수 없고(≤ stop_pct) 보유도 늘릴 수 없다
    (≤ max_hold_min), 익절은 설정의 0.5~2배."""
    return {
        "take_profit_pct": min(
            max(v.take_profit_pct, cfg.take_profit_pct * 0.5), cfg.take_profit_pct * 2
        ),
        "stop_pct": min(v.stop_pct, cfg.stop_pct),
        "max_hold_min": min(v.max_hold_min, cfg.max_hold_min),
    }


def _pos_cfg(cfg: ScalpCfg, pos: dict) -> ScalpCfg:
    over = {k: pos[k] for k in ("take_profit_pct", "stop_pct", "max_hold_min") if k in pos}
    return cfg.model_copy(update=over) if over else cfg


def _enter(
    ctx: ScalpContext,
    ectx: executor.ExecContext,
    *,
    market: str,
    today: date,
    st: dict,
    c: dict,
    price: Decimal,
    time_hms: str,
    reason: str,
    skip: frozenset[str],
    weight_mult: float = 1.0,
    params: dict | None = None,
) -> dict | None:
    cfg = ctx.settings.scalp
    dec = _decision("BUY", c["ticker"], market, price, cfg, reason, weight_mult=weight_mult)
    results = executor.execute(
        ectx,
        dec,
        date=today,
        market=market,
        price=price,
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
        _event(ctx.state, today, market, "entry_rejected", ticker=c["ticker"], reasons=reasons)
        return None
    pos = {
        "ticker": c["ticker"],
        "exchange": c.get("exchange"),
        "weight": cfg.max_weight_pct * weight_mult,  # 일일 손실 계산이 실제 비중을 쓰게
        "entry_price": str(price),
        "entry_time": time_hms,
        "entry_at": ctx.now().isoformat(),
        "qty": qty,
        "reason": reason,
        "dry_run": ctx.dry_run,
        **(params or {}),
    }
    st["open"].append(pos)
    _event(ctx.state, today, market, "entry", **pos)
    ctx.notifier.send(
        messages.scalp(
            leg="entry",
            market=market,
            label=_label(ctx, market, c["ticker"]),
            price=price,
            reason=reason + (" (dry-run)" if ctx.dry_run else ""),
        )
    )
    return pos


def _settle_pending(
    ctx: ScalpContext, ectx, *, market: str, today: date, st: dict, skip: frozenset[str]
) -> list[dict]:
    """지난 틱에 제출한 진입 판정을 회수한다. 판정 없음(재시작·실패·상한)·시간 초과·가격 이탈·
    NO_GO 는 진입하지 않고 그날 그 종목은 끝(done)."""
    cfg = ctx.settings.scalp
    entered: list[dict] = []
    pend: dict = st.setdefault("pending", {})
    now_m = signals.hm_minutes(_local_hms(ctx, market))
    cutoff = now_m >= signals.hm_minutes(ctx.close_hm) - cfg.close_before_min
    for t, p in list(pend.items()):
        key = entry_key(market, today, t)
        age = (ctx.now() - datetime.fromisoformat(p["submitted_at"])).total_seconds()
        v = ctx.judge.poll(key) if ctx.judge is not None else None
        if v == "pending" and age <= cfg.judge_timeout_s and not cutoff:
            continue
        if v == "pending" or age > cfg.judge_timeout_s:
            # 제출 시각 기준 시한: 대기열에서 늦게 끝난 판정도 버린다(그 사이 시장이 움직였다)
            if ctx.judge is not None:
                ctx.judge.discard(key)
            v, why = None, f"판정 시간 초과({cfg.judge_timeout_s:.0f}초)"
        elif cutoff:
            v, why = None, "마감 전 진입 금지 시각 — 판정을 쓰지 않음"
        elif v is None:
            why = "판정 없음(재시작·실패·상한)"
        else:
            why = None
        base = {
            "ticker": t,
            "signal_price": p["signal_price"],
            "signal_time": p["signal_time"],
            "exchange": p.get("exchange"),
        }
        pend.pop(t, None)
        st.setdefault("done", []).append(t)
        if v is None:
            _event(ctx.state, today, market, "judge", go=None, why=why, **base)
            continue
        _event(
            ctx.state,
            today,
            market,
            "judge",
            go=v.go,
            size=v.size,
            confidence=v.confidence,
            why=v.reason,
            **base,
        )
        if not v.go:
            continue
        try:
            q = _quote(ctx, market, t, p.get("exchange"))
        except (KisApiError, ValueError) as e:
            logger.warning("단타 판정 뒤 시세 실패 %s: %s", t, e)
            continue
        sig = Decimal(str(p["signal_price"]))
        if sig > 0 and abs(q / sig - 1) > Decimal(str(cfg.judge_max_drift_pct)):
            _event(ctx.state, today, market, "judge_drift", ticker=t, signal=str(sig), now=str(q))
            continue
        st["done"].remove(t)  # _enter 가 다시 넣는다
        pos = _enter(
            ctx,
            ectx,
            market=market,
            today=today,
            st=st,
            c={"ticker": t, "exchange": p.get("exchange")},
            price=q,
            time_hms=_local_hms(ctx, market),
            reason=f"{p['reason']} · 판정 GO({v.size}): {v.reason}"[:300],
            skip=skip,
            weight_mult=0.5 if v.size == "half" else 1.0,
            params=clamp_verdict(v, cfg) | {"judged": True},
        )
        if pos is not None:
            entered.append(pos)
    return entered


def tick(ctx: ScalpContext, *, market: str, today: date) -> dict:
    """1틱: 청산 → (판정 회수) → 진입. 반환 {entries, exits, skipped?, candidates}."""
    cfg = ctx.settings.scalp
    st = load_state(ctx.state, today, market)
    why = allowed(ctx, market, today, st)
    if why:
        return {"skipped": why}
    judge_on = cfg.judge and ctx.judge is not None
    ectx = _exec_ctx(ctx)
    skip = _skip_accounts(ctx, market)
    now_t = _local_hms(ctx, market)
    out: dict[str, Any] = {"entries": [], "exits": [], "candidates": 0}
    # --- 청산 (손절·익절·마감·최대 보유는 파이썬이 즉시 — 판정자 조언은 조기 청산만) ---
    still_open = []
    for pos in st.get("open") or []:
        try:
            q = _quote(ctx, market, pos["ticker"], pos.get("exchange"))
        except (KisApiError, ValueError) as e:
            logger.warning("단타 시세 실패 %s: %s", pos["ticker"], e)
            still_open.append(pos)
            continue
        reason = signals.exit_reason(
            entry_price=Decimal(str(pos["entry_price"])),
            entry_time=pos["entry_time"],
            price=q,
            now_time=now_t,
            cfg=_pos_cfg(cfg, pos),
            close_hm=ctx.close_hm,
        )
        if reason is None and judge_on:
            key = advice_key(market, pos)
            adv = ctx.judge.poll(key)
            if adv not in (None, "pending") and getattr(adv, "action", None) == "exit":
                reason = "judge_exit"
                _event(ctx.state, today, market, "advice", ticker=pos["ticker"], why=adv.reason)
            elif adv != "pending":
                last = pos.get("advice_at") or pos.get("entry_at")
                due = last is None or ctx.now() - datetime.fromisoformat(last) >= timedelta(
                    minutes=cfg.judge_hold_every_min
                )
                if due:
                    pos["advice_at"] = ctx.now().isoformat()
                    payload = {
                        "kind": "hold",
                        "market": market,
                        "ticker": pos["ticker"],
                        "entry": {"price": pos["entry_price"], "time": pos["entry_time"]},
                        "price": str(q),
                        "limits": _limits(_pos_cfg(cfg, pos)),
                    }
                    try:
                        payload["bars"] = _bars_json(_minutes(ctx, market, pos))
                    except (KisApiError, ValueError):
                        payload["bars"] = []
                    payload |= {
                        "news": _judge_context(ctx, market, pos["ticker"], pos.get("exchange"))[
                            "news"
                        ]
                    }
                    ctx.judge.submit(key, payload, schemas.ScalpAdvice)
        if reason is None:
            still_open.append(pos)
            continue
        if ctx.judge is not None:
            ctx.judge.discard(advice_key(market, pos))
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
            messages.scalp(
                leg="exit",
                market=market,
                label=_label(ctx, market, pos["ticker"]),
                price=q,
                reason=reason + (" (dry-run)" if ctx.dry_run else ""),
                ret=ret,
            )
        )
    st["open"] = still_open
    # 일일 손실 한도: 청산된 거래의 (수익률 × 비중) 합
    realized = sum(
        float(c.get("ret") or 0) * float(c.get("weight") or cfg.max_weight_pct)
        for c in st.get("closed") or []
    )
    if realized <= -cfg.daily_stop_pct and not st.get("halted"):
        st["halted"] = f"실현 {realized:+.2%} ≤ -{cfg.daily_stop_pct:.2%}"
        ctx.notifier.send(messages.scalp(leg="halt", market=market, label="", reason=st["halted"]))
        _event(ctx.state, today, market, "halt", realized=realized)
    # --- 판정 회수 (지난 틱 제출분) ---
    if st.get("pending"):
        if st.get("halted"):
            st["done"] = [*st.get("done", []), *st["pending"]]
            st["pending"] = {}
        else:
            for pos in _settle_pending(ctx, ectx, market=market, today=today, st=st, skip=skip):
                out["entries"].append(
                    {"ticker": pos["ticker"], "price": pos["entry_price"], "qty": pos["qty"]}
                )
    # --- 진입 ---
    if not st.get("halted") and len(st["open"]) + len(st.get("pending") or {}) < cfg.max_positions:
        cands = candidates(ctx, market, today, st)
        out["candidates"] = len(cands)
        for c in cands:
            if len(st["open"]) + len(st.get("pending") or {}) >= cfg.max_positions:
                break
            try:
                bars = _minutes(ctx, market, c)
            except (KisApiError, ValueError) as e:
                logger.warning("단타 분봉 실패 %s: %s", c["ticker"], e)
                continue
            e = signals.entry_signal(bars, cfg, open_hm=ctx.open_hm, close_hm=ctx.close_hm)
            if e is None:
                continue
            if judge_on:  # 판정자에게 묻고 다음 틱에 회수 — 이번 틱엔 진입하지 않는다
                payload = {
                    "kind": "entry",
                    "market": market,
                    "ticker": c["ticker"],
                    "name": _label(ctx, market, c["ticker"]),
                    "signal": {"price": str(e.price), "time": e.time, "reason": e.reason()},
                    "bars": _bars_json(bars),
                    "limits": _limits(cfg),
                } | _judge_context(ctx, market, c["ticker"], c.get("exchange"))
                ctx.judge.submit(
                    entry_key(market, today, c["ticker"]), payload, schemas.ScalpVerdict
                )
                st.setdefault("pending", {})[c["ticker"]] = {
                    "signal_price": str(e.price),
                    "signal_time": e.time,
                    "reason": e.reason(),
                    "exchange": c.get("exchange"),
                    "submitted_at": ctx.now().isoformat(),
                }
                _event(
                    ctx.state, today, market, "judge_submit", ticker=c["ticker"], price=str(e.price)
                )
                continue
            pos = _enter(
                ctx,
                ectx,
                market=market,
                today=today,
                st=st,
                c=c,
                price=e.price,
                time_hms=e.time,
                reason=e.reason(),
                skip=skip,
            )
            if pos is not None:
                out["entries"].append(
                    {"ticker": c["ticker"], "price": str(e.price), "qty": pos["qty"]}
                )
    save_state(ctx.state, today, market, st)
    return out


def hypothetical_return(
    bars: list, *, entry_price: Decimal, entry_time: str, cfg: ScalpCfg, close_hm: str
) -> float | None:
    """NO_GO 판정이 났던 신호를 규칙대로 샀다면의 수익(이후 1분봉으로 청산 규칙 재현). 봉이 모자라면
    마지막 봉 종가로. 신호 뒤 봉이 없으면 None."""
    after = [c for c in bars if (c.time or "") > entry_time]
    if not after or entry_price <= 0:
        return None
    for c in after:
        if signals.exit_reason(
            entry_price=entry_price,
            entry_time=entry_time,
            price=c.close,
            now_time=c.time or "",
            cfg=cfg,
            close_hm=close_hm,
        ):
            return float(c.close / entry_price - 1)
    return float(after[-1].close / entry_price - 1)


def _bars_for(minutes_fn: Callable, ticker: str, exchange: str | None) -> list:
    """minutes_fn(ticker[, exchange]) — 판정 사건에 남긴 거래소를 넘길 수 있으면 넘긴다."""
    try:
        return minutes_fn(ticker, exchange)
    except TypeError:
        return minutes_fn(ticker)


def judge_summary(
    state: Path | None,
    *,
    market: str,
    days: list[date],
    cfg: ScalpCfg,
    close_hm: str = "15:30",
    minutes_fn: Callable[[str], list] | None = None,
    today: date | None = None,
) -> dict:
    """판정자 사후 비교(Phase 25): GO 는 실제 청산 수익, NO_GO 는 오늘분만 `minutes_fn`(당일 분봉)
    으로 가상 수익. 판정 없음(시간 초과·재시작)은 건수만."""
    go_rets: list[float] = []
    nogo_rets: list[float] = []
    counts = {"go": 0, "no_go": 0, "none": 0}
    for d in days:
        ev = state_dir(state, d) / "events.jsonl"
        if not ev.exists():
            continue
        st_p = state_dir(state, d) / f"{market}.json"
        closed = (
            json.loads(st_p.read_text(encoding="utf-8")).get("closed", []) if st_p.exists() else []
        )
        judged = {c["ticker"]: c for c in closed if c.get("judged")}
        for line in ev.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("kind") != "judge" or e.get("market") != market:
                continue
            if e.get("go") is None:
                counts["none"] += 1
            elif e["go"]:
                counts["go"] += 1
                if e["ticker"] in judged:
                    go_rets.append(float(judged[e["ticker"]]["ret"]))
            else:
                counts["no_go"] += 1
                if minutes_fn is not None and d == today:
                    try:
                        r = hypothetical_return(
                            _bars_for(minutes_fn, e["ticker"], e.get("exchange")),
                            entry_price=Decimal(str(e["signal_price"])),
                            entry_time=str(e["signal_time"]),
                            cfg=cfg,
                            close_hm=close_hm,
                        )
                    except Exception:  # noqa: BLE001 - 분봉 실패는 건너뜀
                        r = None
                    if r is not None:
                        nogo_rets.append(r)

    def avg(xs: list[float]) -> float | None:
        return round(sum(xs) / len(xs), 5) if xs else None

    return {
        "counts": counts,
        "go_avg_ret": avg(go_rets),
        "go_trades": len(go_rets),
        "no_go_avg_ret": avg(nogo_rets),
        "no_go_measured": len(nogo_rets),
    }


def report(
    state: Path | None,
    *,
    market: str,
    days: int = 5,
    today: date | None = None,
    judge: dict | None = None,
) -> dict:
    """최근 운영일의 단타 상태 파일을 모아 승률·사유별 집계. judge(={cfg, close_hm, minutes_fn})를
    주면 판정자 GO vs NO_GO 사후 비교(`judge_summary`)를 덧붙인다."""
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
    out = {"market": market, "days": per_day, "summary": summary}
    if judge is not None:
        out["judge"] = judge_summary(
            state,
            market=market,
            days=[date.fromisoformat(x["date"]) for x in per_day],
            today=today,
            **judge,
        )
    return out
