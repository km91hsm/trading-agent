"""분 단위 단타 집행 (Phase 23 S) — **모의 계좌 전용**, 기본 끔.

틱(60초)마다: ⓪ 열린 단타의 청산 점검(허용 판정과 무관 — bear·금지·한도는 신규 진입만 막는다,
전체 검토 O7) → ① 허용 판정(설정·paused·bear 국면·전략가 RISK_OFF/scalp_ok=false·일일 손실
한도) → ② 후보(오늘 `_scan` picks + 새 뉴스 재분석 종목 + 관심종목 − 보유·헤지·오늘 이미 거래) ≤
`max_universe` → ③ 1분봉 진입 규칙(`signals.py`) → ④ `executor.execute(scalp_leg=...)` 로 계좌
모드·dry_run·게이트를 그대로 지키며 주문(실전 계좌는 `skip_accounts`). 포지션은 **계좌별 다리**
(진입·청산 주문 id)로 들고 보유 수량은 원장 `fills` 로 센다(O6): 진입 접수는 `pending_entry`,
체결분만 `held`, 청산 신호면 `closing` — 미체결 진입 BUY 취소 → 계좌별 체결 수량만 SELL(같은
종목의 일반 보유분은 건드리지 않음) → 전부 체결될 때까지 다음 틱에 재시도, 수익률은 체결가 기준.
상태는 `state/scalp/<date>/<market>.json`, 사건은 같은 폴더 `events.jsonl`.
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


def _queue_note(st: dict, **item: Any) -> None:
    """진입·청산 알림은 틱마다 보내지 않고 상태 파일 outbox 에 모은다(전체 검토 T3 — 알림 피로)."""
    st.setdefault("outbox", []).append(item)


def _send_digest(
    notifier: Notifier,
    market: str,
    st: dict,
    *,
    now: datetime,
    every_min: int,
    dry_run: bool,
    force: bool,
) -> bool:
    """outbox 를 `messages.scalp_digest` 한 통으로 보낸다. 그날 첫 묶음은 바로, 그 뒤는
    `digest_every_min` 마다, 중단·마감 청산·허용 해제 때는 즉시(force). 보내지면 비우고 True,
    실패하면 남겨 다음 틱에 다시 보낸다."""
    box = st.get("outbox") or []
    if not box:
        return False
    last = st.get("digest_at")
    if not force and last and (now - datetime.fromisoformat(last)).total_seconds() < every_min * 60:
        return False
    try:
        sent = notifier.send(messages.scalp_digest(market=market, items=box, dry_run=dry_run))
    except Exception:  # noqa: BLE001 - 알림 실패가 단타 상태를 바꾸지 않는다
        logger.warning("단타 묶음 알림 실패", exc_info=True)
        sent = False
    if sent is False:  # 텔레그램은 실패를 False 로 돌려준다 — 묶음을 남겨 다음 틱에 다시
        return False
    st["outbox"] = []
    st["digest_at"] = now.isoformat()
    return True


def _flush_digest(ctx: ScalpContext, market: str, st: dict, *, force: bool = False) -> None:
    _send_digest(
        ctx.notifier,
        market,
        st,
        now=ctx.now(),
        every_min=ctx.settings.scalp.digest_every_min,
        dry_run=ctx.dry_run,
        force=force,
    )


def flush_outbox(
    state: Path | None,
    today: date,
    market: str,
    *,
    notifier: Notifier,
    now: datetime,
    settings: Settings,
    dry_run: bool,
) -> bool:
    """tick 이 돌지 않는 틱(단타 꺼짐·일시정지·창 밖 — 데몬이 tick 전에 건너뜀)에 모아 둔 묶음을
    보낸다(Codex 리뷰: 그날 상태 파일에 갇히면 다음 날엔 다른 파일을 읽어 영영 안 간다)."""
    st = load_state(state, today, market)
    if not st.get("outbox"):
        return False
    every = settings.scalp.digest_every_min
    if not _send_digest(
        notifier, market, st, now=now, every_min=every, dry_run=dry_run, force=True
    ):
        return False
    save_state(state, today, market, st)
    return True


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


def _entry_legs(results: list[dict]) -> dict[str, dict]:
    """계좌별 진입 다리: 접수(accepted·unknown) 주문 id 와 요청 수량. dry_run 승인은 주문 없이
    체결로 본다(`dry`). 거부·실패 계좌는 다리가 없다 — 청산도 그 계좌를 건너뛴다(O6)."""
    out: dict[str, dict] = {}
    for r in results:
        qty = int(r.get("qty") or 0)
        if r.get("error") or not r.get("approved") or qty <= 0:
            continue
        order = r.get("order")
        if order is None:
            if r.get("dry_run"):
                out[r["account_id"]] = {"req": qty, "dry": True, "buy": [], "sell": []}
            continue
        if order.get("status") in ("accepted", "unknown") and order.get("order_id") is not None:
            out[r["account_id"]] = {"req": qty, "buy": [int(order["order_id"])], "sell": []}
    return out


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
    # 하루 종목당 1회(거부돼도) + 임시 포지션(entering)을 주문 전에 남긴다 — 접수 뒤 저장 전에
    # 끊기면 다음 틱이 원장의 진입 주문을 흡수한다(`_adopt_entry_orders`), 다시 사지 않는다
    st.setdefault("done", []).append(c["ticker"])
    pos = {
        "ticker": c["ticker"],
        "exchange": c.get("exchange"),
        "weight": cfg.max_weight_pct * weight_mult,  # 일일 손실 계산이 실제 비중을 쓰게
        "entry_price": str(price),
        "entry_time": time_hms,
        "entry_at": ctx.now().isoformat(),
        "qty": {},
        "legs": {},
        "status": "entering",
        "reason": reason,
        "dry_run": ctx.dry_run,
        **(params or {}),
    }
    st["open"].append(pos)
    save_state(ctx.state, today, market, st)
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
    legs = _entry_legs(results)
    if not legs:
        st["open"].remove(pos)
        save_state(ctx.state, today, market, st)
        reasons = [r.get("reasons") for r in results]
        _event(ctx.state, today, market, "entry_rejected", ticker=c["ticker"], reasons=reasons)
        return None
    pos |= {
        "qty": {a: leg["req"] for a, leg in legs.items()},  # 요청 수량(표시용)
        "legs": legs,
        "status": "held" if ctx.dry_run else "pending_entry",
    }
    save_state(ctx.state, today, market, st)
    _event(ctx.state, today, market, "entry", **pos)
    _queue_note(
        st, leg="entry", label=_label(ctx, market, c["ticker"]), price=str(price), reason=reason
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


# --- 계좌별 체결 상태 (전체 검토 O6) ----------------------------------------------------

EXIT_REQUOTE_S = 120  # 청산 지정가(US)가 이만큼 미체결이면 취소하고 다음 틱에 현재가로 다시 낸다


def _legs(pos: dict) -> dict[str, dict]:
    """포지션의 계좌별 다리. 옛 상태 파일(legs 없음)은 요청 수량을 체결로 보고(`assumed`) 청산
    주문만 추적한다."""
    if "legs" not in pos:
        assumed = "dry" if pos.get("dry_run") else "assumed"
        pos["legs"] = {
            a: {"req": int(q), assumed: True, "buy": [], "sell": []}
            for a, q in (pos.get("qty") or {}).items()
        }
    return pos["legs"]


def _live(status: str) -> bool:
    from trader.ledger import repo

    return status not in repo.DEAD_STATUSES and status != "filled"


def _orders(conn: sqlite3.Connection, ids: list[int]) -> list[dict]:
    """주문 행 + 체결 합계(수량·금액)."""
    out = []
    for oid in ids:
        o = conn.execute(
            "SELECT id, order_no, org_no, qty, status, order_type, created_at FROM orders "
            "WHERE id=?",
            (oid,),
        ).fetchone()
        if o is None:
            continue
        q, amt = 0, Decimal(0)
        for f in conn.execute("SELECT qty, price FROM fills WHERE order_id=?", (oid,)):
            q += int(f["qty"])
            amt += Decimal(str(f["price"])) * int(f["qty"])
        out.append(dict(o) | {"filled": q, "filled_amt": amt})
    return out


def _leg_view(ctx: ScalpContext, leg: dict) -> dict:
    """다리 하나의 체결 현황: 매수·매도 체결 수량/금액, 살아 있는 매수·매도 주문."""
    if leg.get("dry"):
        q = int(leg["req"])
        sold = q if leg.get("dry_sold") else 0
        return {"bought": q, "cost": None, "sold": sold, "proceeds": None, "buys": [], "sells": []}
    buys = _orders(ctx.conn, leg.get("buy") or [])
    sells = _orders(ctx.conn, leg.get("sell") or [])
    assumed = bool(leg.get("assumed"))  # 옛 상태: 진입 체결가를 몰라 신호가로 본다
    return {
        "bought": int(leg["req"]) if assumed else sum(o["filled"] for o in buys),
        "cost": None if assumed else sum((o["filled_amt"] for o in buys), Decimal(0)),
        "sold": sum(o["filled"] for o in sells),
        "proceeds": sum((o["filled_amt"] for o in sells), Decimal(0)),
        "buys": [o for o in buys if _live(o["status"])],
        "sells": [o for o in sells if _live(o["status"])],
    }


def _order_days(ctx: ScalpContext, market: str, pos: dict) -> list[date]:
    """체결을 다시 읽을 현지 날짜: 오늘 + 이월 전 주문일들(전일 주문의 체결·만료도 갱신)."""
    today = ctx.now().astimezone(MARKET_TZ[market]).date()
    days = {date.fromisoformat(x) for x in pos.get("order_days") or []}
    return sorted(days | {today})


def _sync_fills(ctx: ScalpContext, market: str, pos: dict, synced: dict) -> None:
    """살아 있는 주문(또는 취소 뒤 재확인)이 있는 계좌만 체결을 다시 읽는다(틱당 계좌·날짜 1회).
    조회가 전부 성공해야 취소 뒤 재확인(`recheck`)을 지운다."""
    from trader.orchestrator import fills

    for acct, leg in _legs(pos).items():
        if leg.get("dry"):
            continue
        v = _leg_view(ctx, leg)
        if not v["buys"] and not v["sells"] and not leg.get("recheck"):
            continue
        broker = ctx.brokers.get(acct)
        account = next((a for a in ctx.accounts if a.id == acct), None)
        if broker is None or account is None:
            continue
        ok = True
        for d in _order_days(ctx, market, pos):
            key = (acct, d.isoformat())
            if key not in synced:
                try:
                    res = fills.sync_fills(
                        ctx.conn, broker, account, d, now=ctx.now, notifier=ctx.notifier
                    )
                    synced[key] = market not in (res.get("failed_markets") or [])
                except Exception as e:  # noqa: BLE001 - 다음 틱에 다시 읽는다
                    logger.warning("단타 체결 동기화 실패 %s: %s", acct, e)
                    synced[key] = False
            ok = ok and synced[key]
        if ok:
            leg.pop("recheck", None)  # 이번 조회로 취소 뒤 최종 체결을 확인했다


def _cancel(
    ctx: ScalpContext,
    market: str,
    pos: dict,
    acct: str,
    leg: dict,
    o: dict,
    persist: Callable[[], None] = lambda: None,
) -> bool:
    """미체결 잔량 취소. 성공하면 그 다리에 `recheck`(side 목록)를 남긴다 — 취소 직전 체결이
    원장에 아직 없을 수 있어, 다음 틱 체결 동기화로 최종 수량을 확인하기 전에는 그 방향으로
    다리를 끝내거나(BUY) 잔량을 다시 팔지(SELL) 않는다(Codex 리뷰)."""
    from trader.ledger import repo

    broker = ctx.brokers.get(acct)
    if broker is None or not o.get("order_no"):
        return False  # 응답 없는(unknown) 주문은 번호가 없어 취소할 수 없다 → 체결 동기화 대기
    # 재확인 표시는 취소 **전에** 저장한다 — 취소 뒤 저장 전에 끊겨도 다음 틱이 최종 체결을 읽는다
    side = "buy" if int(o["id"]) in (leg.get("buy") or []) else "sell"
    leg["recheck"] = sorted({*(leg.get("recheck") or []), side})
    persist()
    try:
        res = broker.cancel_order(
            str(o["order_no"]),
            market=market,
            org_no=o.get("org_no"),
            ticker=pos["ticker"],
            exchange=pos.get("exchange"),
            qty=int(o["qty"]) - int(o["filled"]),
        )
    except (KisApiError, ValueError) as e:
        logger.warning("단타 미체결 취소 실패 %s %s #%s: %s", acct, pos["ticker"], o["order_no"], e)
        return False
    if res.accepted:
        repo.set_order_status(ctx.conn, int(o["id"]), "cancelled")
        _event(
            ctx.state,
            ctx.now().astimezone(MARKET_TZ[market]).date(),
            market,
            "cancel",
            ticker=pos["ticker"],
            account_id=acct,
            order_id=o["id"],
        )
        return True
    return False


def _close_step(
    ctx: ScalpContext,
    ectx: executor.ExecContext,
    *,
    market: str,
    today: date,
    pos: dict,
    price: Decimal,
    skip: frozenset[str],
    persist: Callable[[], None] = lambda: None,
) -> dict | None:
    """청산 중(closing) 포지션 한 걸음: 미체결 진입 BUY 취소 → 계좌별 체결 보유 수량만 SELL →
    전부 팔렸으면 체결가 기준 결과를 돌려준다(아직이면 None — 다음 틱 재시도). 진입 체결이
    하나도 없이 끝나면 {"void": True}."""
    cfg = ctx.settings.scalp
    legs = _legs(pos)
    now = ctx.now()
    need: dict[str, int] = {}
    waiting = False
    for acct, leg in legs.items():
        if not leg.get("dry"):
            if not _can_trade(ctx, acct):
                waiting = True  # dry-run·일시정지·auto 아닌 계좌: 취소·매도하지 않고 기다린다
                continue
            _adopt_exit_orders(ctx, pos, acct, leg)
        v = _leg_view(ctx, leg)
        for o in v["buys"]:
            if not _cancel(ctx, market, pos, acct, leg, o, persist):
                waiting = True  # 취소되지 않은 진입은 더 체결될 수 있다 — 끝내지 않는다
        for o in v["sells"]:
            age = (now - datetime.fromisoformat(o["created_at"])).total_seconds()
            if o["order_type"] == "LIMIT" and age >= EXIT_REQUOTE_S:
                _cancel(ctx, market, pos, acct, leg, o, persist)
            waiting = True  # 살아 있는 청산 주문: 체결(또는 취소 확인)을 기다린다
        if leg.get("recheck"):
            waiting = True  # 취소 뒤 최종 체결 확인 전에는 끝내지 않는다
        if v["sells"] or "sell" in (leg.get("recheck") or []):
            continue  # 청산 체결을 모르는 채 잔량을 다시 팔면 일반 보유분까지 판다
        rest = v["bought"] - v["sold"]
        if rest > 0:
            need[acct] = rest
    if need:
        dec = _decision("SELL", pos["ticker"], market, price, cfg, pos["exit_reason"])
        results = executor.execute(
            ectx,
            dec,
            date=today,
            market=market,
            price=price,
            exchange=pos.get("exchange"),
            dry_run=ctx.dry_run,
            decision_id=None,
            skip_accounts=skip | frozenset(a.id for a in ctx.accounts if a.id not in need),
            scalp_leg="exit",
            max_qty=need,
        )
        pos.setdefault("exit_results", []).append(
            [{k: r.get(k) for k in ("account_id", "approved", "qty", "reasons")} for r in results]
        )
        for r in results:
            leg = legs.get(r.get("account_id"))
            if leg is None or r.get("error") or not r.get("approved"):
                continue
            order = r.get("order")
            if leg.get("dry"):
                if order is None and r.get("dry_run"):
                    leg["dry_sold"] = True
                    leg["dry_price"] = str(price)
                    need.pop(r["account_id"], None)
            elif order is not None and order.get("order_id") is not None:
                leg.setdefault("sell", []).append(int(order["order_id"]))
        persist()  # 접수한 청산 주문 id 를 바로 남긴다(틱 중간에 끊겨도 다시 내지 않게)
        waiting = waiting or bool(need)
    if waiting:
        return None
    bought = sold = 0
    cost = proceeds = Decimal(0)
    for leg in legs.values():
        v = _leg_view(ctx, leg)
        if v["cost"] is None:
            v["cost"] = Decimal(str(pos["entry_price"])) * v["bought"]
        if v["proceeds"] is None:
            v["proceeds"] = Decimal(str(leg.get("dry_price") or price)) * v["sold"]
        bought += v["bought"]
        sold += v["sold"]
        cost += v["cost"]
        proceeds += v["proceeds"]
    if bought == 0 or sold == 0:
        return {"void": True}
    entry_px = cost / bought
    exit_px = proceeds / sold
    return {
        "entry_fill": _px(entry_px),
        "exit_price": _px(exit_px),
        "filled": {a: _leg_view(ctx, leg)["bought"] for a, leg in legs.items()},
        "ret": round(float(exit_px / entry_px - 1), 5),
    }


def _can_trade(ctx: ScalpContext, acct: str) -> bool:
    """실제 주문 다리를 건드려도 되는가: dry-run 아님·일시정지 아님·계좌 실효 모드 auto. 그 밖이면
    취소도 매도도 하지 않는다 — 승인 요청을 틱마다 쌓거나 안전장치를 우회하지 않게(Codex 리뷰)."""
    if ctx.dry_run or control.is_paused(ctx.conn):
        return False
    account = next((a for a in ctx.accounts if a.id == acct), None)
    return account is not None and executor.effective_mode(ctx.conn, account)[0] == "auto"


def _adopt_exit_orders(ctx: ScalpContext, pos: dict, acct: str, leg: dict) -> None:
    """청산 시작 뒤 이 계좌·종목에 낸 결정 없는 SELL(외부 수동 주문 제외) 중 다리에 없는 것을
    흡수한다 — 접수와 상태 저장 사이에 끊긴 경우 같은 잔량을 다시 팔지 않게(Codex 리뷰)."""
    since = pos.get("closing_at")  # 청산 시작은 주문 전에 저장된다
    if not since:
        return
    known = set(leg.get("sell") or [])
    for r in ctx.conn.execute(
        "SELECT id FROM orders WHERE account_id=? AND ticker=? AND side='SELL' "
        "AND decision_id IS NULL AND mode != 'external' AND created_at >= ? ORDER BY id",
        (acct, pos["ticker"], since),
    ):
        if int(r["id"]) not in known:
            leg.setdefault("sell", []).append(int(r["id"]))
            logger.warning("단타 청산 주문 흡수 %s %s #%s", acct, pos["ticker"], r["id"])


def _adopt_entry_orders(ctx: ScalpContext, pos: dict) -> None:
    """임시 포지션(entering)의 진입 주문을 원장에서 찾아 다리로 만든다(진입 뒤 결정 없는 BUY)."""
    legs: dict[str, dict] = {}
    for r in ctx.conn.execute(
        "SELECT id, account_id, qty FROM orders WHERE ticker=? AND side='BUY' "
        "AND decision_id IS NULL AND mode != 'external' AND created_at >= ? ORDER BY id",
        (pos["ticker"], pos["entry_at"]),
    ):
        leg = legs.setdefault(r["account_id"], {"req": 0, "buy": [], "sell": []})
        leg["req"] += int(r["qty"])
        leg["buy"].append(int(r["id"]))
    pos |= {
        "legs": legs,
        "qty": {a: leg["req"] for a, leg in legs.items()},
        "status": "pending_entry",
    }
    logger.warning("단타 진입 주문 흡수 %s %s", pos["ticker"], sorted(legs))


def _px(v: Decimal) -> str:
    """체결 평균가 표기: 소수 4자리 이하, 지수 표기 없이."""
    return format(v.quantize(Decimal("0.0001")).normalize(), "f")


def _check_exits(
    ctx: ScalpContext,
    ectx: executor.ExecContext,
    *,
    market: str,
    today: date,
    st: dict,
    skip: frozenset[str],
    out: dict,
) -> None:
    persist = lambda: save_state(ctx.state, today, market, st)  # noqa: E731
    """보유·진입 대기 단타의 청산 점검(허용 판정과 무관하게 매 틱 — O7). 손절·익절·마감·최대
    보유는 파이썬이 즉시, 판정자 조언은 조기 청산만."""
    cfg = ctx.settings.scalp
    judge_on = cfg.judge and ctx.judge is not None
    now_t = _local_hms(ctx, market)
    synced: dict = {}
    for pos in list(st.get("open") or []):  # 끝난 포지션은 바로 빼서 중간 저장에 남지 않게
        if pos.get("status") == "entering":  # 진입 주문 뒤 저장 전에 끊겼다
            _adopt_entry_orders(ctx, pos)
        _legs(pos)
        if not ctx.dry_run:
            _sync_fills(ctx, market, pos, synced)
        views = {a: _leg_view(ctx, leg) for a, leg in pos["legs"].items()}
        bought = sum(v["bought"] for v in views.values())
        live_buys = any(v["buys"] for v in views.values())
        if pos.get("status") != "closing":
            if bought == 0 and not live_buys:  # 진입이 체결 없이 죽었다(거부·만료·취소)
                st["open"].remove(pos)
                _event(ctx.state, today, market, "entry_void", ticker=pos["ticker"])
                if ctx.judge is not None:
                    ctx.judge.discard(advice_key(market, pos))
                continue
            pos["status"] = "held" if bought > 0 else "pending_entry"
            if bought > 0:
                pos["filled"] = {a: v["bought"] for a, v in views.items()}
                costs = [v for v in views.values() if v["cost"] is not None and v["bought"] > 0]
                if costs:  # 청산 규칙은 실제 체결 평균가 기준(신호가 아님)
                    pos["entry_fill"] = _px(
                        sum((v["cost"] for v in costs), Decimal(0))
                        / sum(v["bought"] for v in costs)
                    )
        try:
            q = _quote(ctx, market, pos["ticker"], pos.get("exchange"))
        except (KisApiError, ValueError) as e:
            logger.warning("단타 시세 실패 %s: %s", pos["ticker"], e)
            continue
        if pos.get("status") != "closing":
            reason = signals.exit_reason(
                entry_price=Decimal(str(pos.get("entry_fill") or pos["entry_price"])),
                entry_time=pos["entry_time"],
                price=q,
                now_time=now_t,
                cfg=_pos_cfg(cfg, pos),
                close_hm=ctx.close_hm,
            )
            if reason is None and judge_on:
                reason = _advice(ctx, market, today, pos, q)
            if reason is None:
                continue
            if ctx.judge is not None:
                ctx.judge.discard(advice_key(market, pos))
            pos |= {
                "status": "closing",
                "exit_reason": reason,
                "exit_time": now_t,
                "closing_at": ctx.now().isoformat(),
            }
            persist()
            _event(ctx.state, today, market, "closing", ticker=pos["ticker"], reason=reason)
        res = _close_step(
            ctx, ectx, market=market, today=today, pos=pos, price=q, skip=skip, persist=persist
        )
        if res is None:
            continue
        st["open"].remove(pos)
        if res.get("void"):
            _event(ctx.state, today, market, "entry_void", ticker=pos["ticker"])
            continue
        reason = pos["exit_reason"]
        closed = {k: v for k, v in pos.items() if k not in ("status",)} | {
            "exit_price": res["exit_price"],
            "entry_fill": res["entry_fill"],
            "filled": res["filled"],
            "reason": reason,
            "ret": res["ret"],
        }
        st.setdefault("closed", []).append(closed)
        out["exits"].append({"ticker": pos["ticker"], "reason": reason, "ret": res["ret"]})
        _event(ctx.state, today, market, "exit", **closed)
        _queue_note(
            st,
            leg="exit",
            label=_label(ctx, market, pos["ticker"]),
            price=res["exit_price"],
            reason=reason,
            ret=res["ret"],
        )


def _advice(ctx: ScalpContext, market: str, today: date, pos: dict, q: Decimal) -> str | None:
    """판정자 보유 조언: 지난 조언이 exit 면 "judge_exit", 아니면 주기가 됐을 때 새로 묻는다."""
    cfg = ctx.settings.scalp
    key = advice_key(market, pos)
    adv = ctx.judge.poll(key)
    if adv not in (None, "pending") and getattr(adv, "action", None) == "exit":
        _event(ctx.state, today, market, "advice", ticker=pos["ticker"], why=adv.reason)
        return "judge_exit"
    if adv == "pending":
        return None
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
        payload |= {"news": _judge_context(ctx, market, pos["ticker"], pos.get("exchange"))["news"]}
        ctx.judge.submit(key, payload, schemas.ScalpAdvice)
    return None


CARRY_DAYS = 14  # 이만큼 거슬러 미종결 단타를 찾는다(주말·연휴 포함)


def _carry_over(ctx: ScalpContext, market: str, today: date, st: dict) -> None:
    """지난 운영일 상태 파일의 미종결 단타(마감까지 못 판 잔량·unknown·일시정지)를 오늘로 옮겨
    곧바로 청산을 이어 간다(Codex 리뷰). 매 틱 본다 — 오늘 파일을 먼저 쓰고 옛 파일을 비우므로
    그 사이에 끊겨도 다음 틱이 (종목, 진입 시각) 으로 중복 없이 마저 옮긴다."""
    have = {f"{p['ticker']}|{p.get('entry_at')}" for p in st.get("open") or []}
    have |= set(st.get("carried") or [])  # 이미 승계해 끝낸 것도(옛 파일이 다시 보여도) 건너뛴다
    for i in range(1, CARRY_DAYS + 1):
        d = today - timedelta(days=i)
        p = state_dir(ctx.state, d) / f"{market}.json"
        if not p.exists():
            continue
        old = load_state(ctx.state, d, market)
        if not old.get("open"):
            continue
        for pos in old["open"]:
            pos["order_days"] = sorted({*(pos.get("order_days") or []), d.isoformat()})
            key = f"{pos['ticker']}|{pos.get('entry_at')}"
            if key in have:
                continue
            st.setdefault("carried", []).append(key)
            if pos.get("status") == "entering":  # 진입 접수 뒤 저장 전에 끊긴 채 넘어왔다
                _adopt_entry_orders(ctx, pos)
            if pos.get("status") != "closing":
                pos |= {
                    "status": "closing",
                    "exit_reason": "carry_over",
                    "exit_time": _local_hms(ctx, market),
                    "closing_at": ctx.now().isoformat(),
                }
            pos["carried_from"] = d.isoformat()
            st["open"].append(pos)
            st.setdefault("done", []).append(pos["ticker"])
            _event(ctx.state, today, market, "carry_over", ticker=pos["ticker"], since=str(d))
        old["open"] = []
        save_state(ctx.state, today, market, st)  # 오늘 파일을 먼저 — 끊겨도 잃지 않게
        save_state(ctx.state, d, market, old)


def tick(ctx: ScalpContext, *, market: str, today: date) -> dict:
    """1틱: 청산 → (판정 회수) → 진입. 반환 {entries, exits, skipped?, candidates}.
    청산은 허용 판정(bear·전략가 금지·일일 손실·일시정지)보다 먼저 돈다 — 그 제한은 신규 진입만
    막는다(전체 검토 O7). 일시정지면 executor 가 주문을 내지 않아 포지션은 closing 으로 남는다."""
    cfg = ctx.settings.scalp
    st = load_state(ctx.state, today, market)
    _carry_over(ctx, market, today, st)
    ectx = _exec_ctx(ctx)
    skip = _skip_accounts(ctx, market)
    out: dict[str, Any] = {"entries": [], "exits": [], "candidates": 0}
    had_state = bool(st.get("open") or st.get("pending"))
    if st.get("open"):
        _check_exits(ctx, ectx, market=market, today=today, st=st, skip=skip, out=out)
    # 일일 손실 한도: 청산된 거래의 (수익률 × 비중) 합
    realized = sum(
        float(c.get("ret") or 0) * float(c.get("weight") or cfg.max_weight_pct)
        for c in st.get("closed") or []
    )
    if realized <= -cfg.daily_stop_pct and not st.get("halted"):
        st["halted"] = f"실현 {realized:+.2%} ≤ -{cfg.daily_stop_pct:.2%}"
        had_state = True
        ctx.notifier.send(messages.scalp(leg="halt", market=market, label="", reason=st["halted"]))
        _event(ctx.state, today, market, "halt", realized=realized)
    why = allowed(ctx, market, today, st)
    if why:  # 신규 진입만 막는다: 판정 대기 신호도 버린다(그날 그 종목은 끝)
        if st.get("pending"):
            for t in st["pending"]:
                if ctx.judge is not None:
                    ctx.judge.discard(entry_key(market, today, t))
            st["done"] = [*st.get("done", []), *st["pending"]]
            st["pending"] = {}
        if st.get("outbox"):  # 진입이 막히면 다음 묶음 시점이 없을 수 있다 — 청산 묶음을 지금
            _flush_digest(ctx, market, st, force=True)
            had_state = True
        if had_state:
            save_state(ctx.state, today, market, st)
        return out | {"skipped": why}
    judge_on = cfg.judge and ctx.judge is not None
    # --- 판정 회수 (지난 틱 제출분) ---
    if st.get("pending"):
        for pos in _settle_pending(ctx, ectx, market=market, today=today, st=st, skip=skip):
            out["entries"].append(
                {"ticker": pos["ticker"], "price": pos["entry_price"], "qty": pos["qty"]}
            )
    # --- 진입 ---
    if len(st["open"]) + len(st.get("pending") or {}) < cfg.max_positions:
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
    now_m = signals.hm_minutes(_local_hms(ctx, market))
    closing = now_m >= signals.hm_minutes(ctx.close_hm) - cfg.close_before_min
    _flush_digest(ctx, market, st, force=bool(st.get("halted")) or closing)
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
