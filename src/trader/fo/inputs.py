"""파생 세션 입력(Phase 26 단계 3, 스펙 §3). 현물 `sources/inputs.build_inputs` 의 파생판 —
절(section)마다 `{data, received_at, asof, error}` 로 감싸고 예외를 던지지 않는다(`errors` 에 모음).
저장은 시장 디렉터리의 `_fo_inputs`, 서브에이전트 묶음은 `bundle()` 이 각 md 의 입력 형태로
어댑트한다(기술·수급 애널리스트는 현물 `technical`·`sentiment` 절 모양, 거시 애널리스트는 시장
세션 `_market_inputs` 모양, 트레이더는 fo-trader md 의 `context`).

지수(코스피200)는 응답의 시장 시각(`index_asof`)으로 신선도를 보고, 지수 절이 실패해도 시세의
기초지수로 채우지 않는다(N1). 옵션 체인은 ATM ± `CHAIN_STRIKES`(4) 행사가 × 콜·풋 ≤ 18 시세."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from trader.broker import fo_master
from trader.broker.fo import CONTRACT_VERSION
from trader.broker.models import drop_incomplete_tail
from trader.config import KST, AccountConfig, KisCfg
from trader.control import control
from trader.fo import rules, store
from trader.fo.executor import VEHICLES, FoExecContext, verified_key
from trader.orchestrator import run_store
from trader.orchestrator.run_store import write_checkpoint
from trader.risk import regime as regime_mod
from trader.sources import indicators
from trader.sources.base import Section, run_section, skipped_section
from trader.sources.inputs import _jsonable

logger = logging.getLogger("trader.fo")

MARKET = "KR"
PROXY = "KOSPI200"
CHAIN_STRIKES = 4  # ATM 양쪽 행사가 수 → (2·4+1)·2 = 18 시세
INPUTS_STAGE = "_fo_inputs"
INDEX_PROXY_TICKER = "069500"  # 수급 절의 지수 proxy ETF(KODEX 200) — 선물 수급 TR 미확정

BundleKind = Literal["analyst", "researcher", "trader"]


def _wrap_section(data: Any, *, asof: datetime, now: datetime, error: str | None = None) -> dict:
    return {
        "data": data,
        "received_at": now.isoformat(),
        "asof": asof.isoformat(),
        "error": error,
    }


def _no_quote(_: fo_master.Contract) -> date | None:
    return None


def _front(ctx: FoExecContext, today: date) -> fo_master.Contract | None:
    cfg = ctx.settings.fo
    return fo_master.front_month(
        ctx.master, cfg.product_cfg(), "FUTURES", today=today, roll_days=cfg.roll_days,
        last_trade_date_of=_no_quote,
    )  # fmt: skip


# --- 절 -----------------------------------------------------------------------------------


def _daily_days() -> int:
    """일봉 수는 `sources.yaml kis.daily_days`(없으면 모델 기본값 — 지표 워밍업 260봉)."""
    try:
        from trader.config import load_sources

        return int(load_sources().kis.daily_days)
    except Exception:  # noqa: BLE001 — sources.yaml 이 없거나 깨져도 수집은 간다
        return KisCfg().daily_days


def _technical(
    ctx: FoExecContext, front: fo_master.Contract | None, *, asof: datetime, now_fn
) -> dict[str, Any]:
    keys = ("quote", "daily", "minute", "orderbook", "indicators")
    cfg = ctx.settings
    days = _daily_days()
    mc = ctx.market_client
    meta: dict[str, Any] = {
        "code": front.code if front else None,
        "name": front.name if front else None,
        "product": cfg.fo.product,
        "asof": asof.isoformat(),
        "last_candle_date": None,
    }
    if mc is None or front is None:
        why = "no market client" if mc is None else "no front contract"
        return {**{k: skipped_section(why, now=now_fn) for k in keys}, "meta": meta}
    # 봉 컷은 KST 운영일·KST 시각(KIS 봉의 날짜·체결시각이 현지, S3 P1) — UTC 날짜로 자르면 KST
    # 자정 뒤 09:00 전 세션이 오늘 봉을 잃는다
    cutoff = run_store.run_date_for(MARKET, asof)
    cutoff_hms = asof.astimezone(KST).strftime("%H%M%S")
    candles: list = []
    code, kind = front.code, front.kind

    def minute_ok(c) -> bool:
        if c.date < cutoff:
            return True
        return c.date == cutoff and (c.time is None or c.time <= cutoff_hms)

    def daily() -> list[dict]:
        nonlocal candles
        got = mc.get_fo_daily(code, kind, days=days)
        candles = drop_incomplete_tail([c for c in got if c.date <= cutoff])
        if not candles:
            raise ValueError("일봉이 비어 있습니다")
        meta["last_candle_date"] = candles[-1].date.isoformat()
        return _jsonable(candles)

    out: dict[str, Any] = {
        "quote": run_section(lambda: _jsonable(mc.get_fo_price(code, kind)), now=now_fn),
        "daily": run_section(daily, now=now_fn),
        "minute": run_section(
            lambda: _jsonable([c for c in mc.get_fo_minute(code, kind) if minute_ok(c)]),
            now=now_fn,
        ),
        "orderbook": run_section(
            lambda: _jsonable(mc.get_fo_orderbook(code, kind)), now=now_fn
        ),
    }
    out["indicators"] = (
        run_section(lambda: indicators.latest(candles), now=now_fn)
        if out["daily"]["error"] is None
        else skipped_section("no daily candles", now=now_fn)
    )
    out["meta"] = meta
    return out


def _sentiment(ctx: FoExecContext, quote: dict | None, chain: list[dict], *, now_fn) -> dict:
    mc = ctx.market_client
    gaps: list[str] = []
    if mc is None:
        flow: Section = skipped_section("no market client", now=now_fn)
    else:
        flow = run_section(
            lambda: _jsonable(mc.get_investor_kr(INDEX_PROXY_TICKER, days=20)), now=now_fn
        )
    if flow["error"]:
        gaps.append(f"investor_flow: {flow['error']}")
    oi = (quote or {}).get("open_interest")
    if oi is None:
        gaps.append("open_interest 없음")
    call_v = sum(int(c.get("volume") or 0) for c in chain if c.get("kind") == "CALL")
    put_v = sum(int(c.get("volume") or 0) for c in chain if c.get("kind") == "PUT")
    pcr = round(put_v / call_v, 3) if call_v > 0 else None
    if pcr is None:
        gaps.append("put_call_ratio 계산 불가(콜 거래량 0 또는 체인 없음)")
    return {
        "investor_flow": flow,
        "investor_flow_proxy": INDEX_PROXY_TICKER,
        "open_interest": oi,
        "put_call_ratio": pcr,
        "chain_volume": {"call": call_v, "put": put_v},
        "community": skipped_section("not collected for fo", now=now_fn),
        "manual_inputs": skipped_section("no manual input", now=now_fn),
        "data_gaps": gaps,
    }


def _news_macro(
    ctx: FoExecContext, *, today: date, asof: datetime, accounts: list[AccountConfig], now_fn
) -> tuple[dict, str | None]:
    """시장 세션(`market_view.build_inputs`)의 거시 절을 그대로 재사용한다(국면·지수 proxy 봉·거시
    지표·거시 뉴스·시장 메모·세이브티커·헤지 보유). 헤지 보유는 **현물** 계좌·브로커
    (`ctx.spot_accounts`·`ctx.spot_brokers`, 호출자가 넘긴 현물 계좌 포함)로만 읽는다 — fo
    브로커에는 ETF 잔고가 없다(S3 P1)."""
    from trader.orchestrator import market_view

    spot = {a.id: a for a in [*ctx.spot_accounts, *accounts] if a.asset != "fo"}
    brokers = {aid: ctx.spot_brokers[aid] for aid in spot if aid in ctx.spot_brokers}
    try:
        data = market_view.build_inputs(
            ctx.conn, MARKET, today, settings=ctx.settings, asof=asof,
            market_client=ctx.market_client, accounts=list(spot.values()), brokers=brokers,
            state=ctx.state, now=now_fn, secrets=ctx.secrets,
        )  # fmt: skip
    except Exception as e:  # noqa: BLE001 — 거시 절 실패도 세션은 띄운다
        return {}, f"{type(e).__name__}: {e}"
    return data, None


def _option_chain(
    ctx: FoExecContext, underlying: Decimal | None, *, today: date
) -> tuple[list[dict], str | None, str | None]:
    """가까운 월물 ATM ± CHAIN_STRIKES. (체인, 월물, 오류)."""
    mc = ctx.market_client
    cfg = ctx.settings.fo
    if mc is None:
        return [], None, "no market client"
    if underlying is None:
        return [], None, "지수 없음(체인의 ATM 을 정할 수 없다)"
    ym, chain = fo_master.option_front(
        ctx.master, cfg.product_cfg(), today=today, roll_days=cfg.roll_days,
        last_trade_date_of=_no_quote,
    )  # fmt: skip
    if not ym:
        return [], None, "roll 창 밖의 옵션 월물 없음"
    ks = sorted({c.strike for c in chain if c.strike is not None})
    if not ks:
        return [], ym, f"옵션 월물 {ym} 에 행사가 없음"
    atm = min(range(len(ks)), key=lambda i: abs(ks[i] - underlying))
    wanted = ks[max(0, atm - CHAIN_STRIKES) : atm + CHAIN_STRIKES + 1]
    try:
        quotes, errs = mc.get_option_chain(chain, strikes=wanted)
    except Exception as e:  # noqa: BLE001
        return [], ym, f"{type(e).__name__}: {e}"
    err = f"시세 실패 {len(errs)}건: {', '.join(errs[:5])}" if errs else None
    return [_jsonable(q) for q in quotes], ym, err


def _account_summary(
    ctx: FoExecContext, a: AccountConfig, *, today: date
) -> tuple[dict, str | None]:
    broker = ctx.brokers.get(a.id)
    out: dict[str, Any] = {
        "id": a.id, "env": a.env, "mode": a.mode, "nav": None, "orderable_cash": None,
        "margin_total": None, "add_margin": None, "realized_today": None, "received_at": None,
        "unverified": [], "positions": [], "legs": [],
    }  # fmt: skip
    err = None
    if broker is None:
        err = "broker 없음"
    else:
        try:
            bal = broker.get_balance()
            c = bal.cash
            out.update(
                nav=_jsonable(c.nav), orderable_cash=str(c.orderable_cash),
                margin_total=str(c.margin_total), add_margin=str(c.add_margin),
                realized_today=str(c.realized_today), received_at=c.received_at.isoformat(),
                unverified=[u.code for u in bal.unverified],
                broker_positions=[_jsonable(p) for p in bal.positions],
            )  # fmt: skip
        except Exception as e:  # noqa: BLE001
            err = f"balance: {type(e).__name__}: {e}"
    out["positions"] = [
        {
            k: _jsonable(getattr(p, k))
            for k in (
                "id", "code", "kind", "contract_side", "direction", "vehicle", "status",
                "qty_target", "qty_held", "leverage_used", "stop_pt", "stop_fut", "tp_pt",
                "approved_mult", "premium_stop", "entry_premium", "nav_at_open",
                "realized_loss", "fees", "horizon_until", "last_trade_date", "version",
            )
        }  # fmt: skip
        for p in store.open_positions(ctx.conn, a.id)
    ]
    out["legs"] = [
        {k: _jsonable(getattr(x, k)) for k in ("id", "position_id", "intent", "status", "qty",
                                               "filled_qty", "price")}  # fmt: skip
        for x in store.live_legs(ctx.conn, account_id=a.id)
    ]
    out["halted"] = control.get_flag(ctx.conn, f"fo_halted:{a.id}") == today.isoformat()
    return out, err


def _verified(ctx: FoExecContext, accounts: list[AccountConfig]) -> dict[str, dict[str, bool]]:
    s = ctx.settings
    return {
        a.id: {
            v: control.get_flag(ctx.conn, verified_key(s, a, v)) == CONTRACT_VERSION
            for v in VEHICLES
        }
        for a in accounts
    }


def _limits(ctx: FoExecContext, regime_state: str | None) -> dict[str, Any]:
    cfg = ctx.settings.fo
    o = cfg.option
    return {
        "enabled": cfg.enabled,
        "product": cfg.product,
        "vehicles": list(cfg.vehicles),
        "max_leverage": cfg.max_leverage,
        "regime_cap": cfg.regime_cap(regime_state or "neutral"),
        "max_contracts": cfg.max_contracts,
        "max_positions": cfg.max_positions,
        "risk_per_trade_pct": cfg.risk_per_trade_pct,
        "overnight_risk_pct": cfg.overnight_risk_pct,
        "min_confidence": cfg.min_confidence,
        "min_stop_pt": cfg.min_stop_pt,
        "roll_days": cfg.roll_days,
        "hold_overnight": cfg.hold_overnight,
        "entry_window": {"start": cfg.entry_window.start, "cutoff": cfg.entry_window.cutoff},
        "cross_check": cfg.cross_check,
        "option": {
            "min_days_left": o.min_days_left,
            "allowed_strikes": list(o.allowed_strikes),
            "sell_stop_mult": {"min": o.sell_stop_mult.min, "default": o.sell_stop_mult.default,
                               "max": o.sell_stop_mult.max},  # fmt: skip
            "max_premium_pct": o.max_premium_pct,
            "buy_stop_pct": o.buy_stop_pct,
            "sell_against_regime": o.sell_against_regime,
        },
    }


# --- 조립 -----------------------------------------------------------------------------------


def build_inputs(
    ctx: FoExecContext,
    *,
    today: date,
    asof: datetime,
    accounts: list[AccountConfig],
    spot_hedge_held: list[str] | None = None,
) -> dict:
    """파생 세션 입력 하나. 절: `technical`·`sentiment`·`news_macro`·`context` + `meta`·`errors`.
    `meta.historical` 은 `asof` 가 `now − fo.max_input_age_s` 보다 과거(세션은 dry-run 강제,
    제출은 기록만). `spot_hedge_held` 는 현물 계좌의 헤지 ETF 보유(없으면 거시 절의
    `hedge.held`)."""
    now_fn: Callable[[], datetime] = ctx.now
    now = now_fn()
    cfg = ctx.settings.fo
    errors: dict[str, str] = {}
    fo_accounts = [a for a in accounts if a.asset == "fo"]
    historical = asof < now - timedelta(seconds=cfg.max_input_age_s)

    front = _front(ctx, today)
    tech = _technical(ctx, front, asof=asof, now_fn=now_fn)
    for k in ("quote", "daily", "minute", "orderbook", "indicators"):
        if tech[k]["error"] and tech[k]["error"] not in ("no daily candles",):
            errors[f"technical.{k}"] = tech[k]["error"]
    quote = tech["quote"]["data"] if tech["quote"]["error"] is None else None

    # 지수: 응답의 시장 시각으로(시세의 기초지수로 대체하지 않는다, N1)
    underlying: Decimal | None = None
    index_asof: datetime | None = None
    mc = ctx.market_client
    if mc is None:
        errors["context.index"] = "no market client"
    else:
        try:
            underlying, index_asof = mc.get_kospi200()
        except Exception as e:  # noqa: BLE001
            errors["context.index"] = f"{type(e).__name__}: {e}"
    chain, chain_ym, chain_err = _option_chain(ctx, underlying, today=today)
    chain_received = now_fn().isoformat()
    if chain_err:
        errors["context.option_chain"] = chain_err

    sent = _sentiment(ctx, quote, chain, now_fn=now_fn)
    if sent["investor_flow"]["error"]:
        errors["sentiment.investor_flow"] = sent["investor_flow"]["error"]

    macro, macro_err = _news_macro(ctx, today=today, asof=asof, accounts=accounts, now_fn=now_fn)
    if macro_err:
        errors["news_macro"] = macro_err
    for k, v in (macro.get("errors") or {}).items():
        errors[f"news_macro.{k}"] = v

    regime = regime_mod.load(ctx.conn, MARKET) or {}
    view = ctx.view_of() if ctx.view_of is not None else None
    if view is None:
        from trader.orchestrator import tools

        view = tools.market_view_today(ctx.conn, MARKET, today)
    view_state = rules.view_state_of(
        view, today=today, min_confidence=ctx.settings.hedge.agent_min_confidence
    )
    acct_rows: list[dict] = []
    for a in fo_accounts:
        row, err = _account_summary(ctx, a, today=today)
        if err:
            errors[f"context.accounts.{a.id}"] = err
        acct_rows.append(row)
    pcfg = cfg.product_cfg()
    context = {
        "index": {
            "price": _jsonable(underlying),
            "asof": index_asof.isoformat() if index_asof else None,
        },
        "index_asof": index_asof.isoformat() if index_asof else None,
        "option_chain": chain,
        "option_expiry_ym": chain_ym,
        "chain_received_at": chain_received,
        "products": {
            "product": cfg.product, "name": pcfg.name, "multiplier": pcfg.multiplier,
            "futures_tick": str(pcfg.futures_tick), "futures_code": front.code if front else None,
        },  # fmt: skip
        "accounts": acct_rows,
        "regime": {
            "state": regime.get("state"), "count": regime.get("count"),
            "since": regime.get("since"),
        },  # fmt: skip
        "market_view": {
            "state": view_state,
            **({k: view.get(k) for k in ("stance", "confidence", "rationale", "date")}
               if view else {}),  # fmt: skip
        },
        "spot_exposure": {
            "hedge_held": spot_hedge_held
            if spot_hedge_held is not None
            else ((macro.get("hedge") or {}).get("held") or []),
        },
        "costs": dict(cfg.costs_bp),
        "limits": _limits(ctx, regime.get("state")),
        "verified": _verified(ctx, fo_accounts),
    }
    return {
        "schema_version": 1,
        "meta": {
            "market": MARKET,
            "date": today.isoformat(),
            "asof": asof.isoformat(),
            "collected_at": now.isoformat(),
            "proxy": PROXY,
            "historical": historical,
            "max_input_age_s": cfg.max_input_age_s,
            "request_id": uuid.uuid4().hex,
            "futures_code": front.code if front else None,
        },
        "technical": _wrap_section(tech, asof=asof, now=now_fn()),
        "sentiment": _wrap_section(sent, asof=asof, now=now_fn()),
        "news_macro": _wrap_section(macro, asof=asof, now=now_fn(), error=macro_err),
        "context": _wrap_section(context, asof=asof, now=now_fn()),
        "errors": errors,
    }


def save_inputs(inputs: dict, *, today: date, state: Path | None) -> Path:
    d = run_store.market_dir(today, MARKET, state, create=True)
    return write_checkpoint(d, INPUTS_STAGE, run_store.checkpoint_payload(inputs, None))


# --- 묶음 -----------------------------------------------------------------------------------


def bundle(
    inputs: dict,
    *,
    kind: BundleKind,
    role: str | None = None,
    round: int | None = None,
    side: str | None = None,
    reports: list[dict] | None = None,
    summary: dict | None = None,
    position: dict | None = None,
    previous: tuple[dict | None, dict | None] | None = None,
    trigger: dict | None = None,
) -> dict:
    """서브에이전트 입력 묶음(크기 제한은 호출자 `tools._shrink`). kind=analyst(role 필수) |
    researcher(round·side·reports·previous) | trader(reports·summary·position·trigger)."""
    meta = inputs.get("meta") or {}
    base = {"scope": "fo", "market": MARKET, "date": meta.get("date")}
    ctx = (inputs.get("context") or {}).get("data") or {}
    if kind == "analyst":
        if role == "news_macro":
            return {**base, **((inputs.get("news_macro") or {}).get("data") or {})}
        head = {
            **base, "ticker": PROXY, "asof": meta.get("asof"), "role": role,
            "input_errors": inputs.get("errors") or {},
        }  # fmt: skip
        if role == "technical":
            return {**head, "technical": _without_meta((inputs.get("technical") or {}).get("data"))}
        if role == "sentiment":
            tech = (inputs.get("technical") or {}).get("data") or {}
            return {
                **head, "sentiment": (inputs.get("sentiment") or {}).get("data") or {},
                "quote": tech.get("quote"),
            }  # fmt: skip
        raise ValueError(f"알 수 없는 role: {role} (technical|sentiment|news_macro)")
    if kind == "researcher":
        own, opp = previous or (None, None)
        return {
            **base, "proxy": PROXY, "round": round, "side": side, "reports": reports or [],
            "regime": ctx.get("regime"), "market_view": ctx.get("market_view"),
            "index": ctx.get("index"), "own_previous": own, "opponent_previous": opp,
        }  # fmt: skip
    if kind == "trader":
        return {
            **base, "proxy": PROXY, "reports": reports or [], "research_summary": summary,
            "context": {**ctx, "position": position}, "request_id": meta.get("request_id"),
            "trigger": trigger, "input_errors": inputs.get("errors") or {},
            "historical": meta.get("historical", False),
        }  # fmt: skip
    raise ValueError(f"알 수 없는 kind: {kind} (analyst|researcher|trader)")


def _without_meta(d: dict | None) -> dict:
    return {k: v for k, v in (d or {}).items() if k != "meta"}
