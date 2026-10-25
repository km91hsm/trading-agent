"""시장 세션 (Phase 18): 거시 애널리스트 → 강세/약세 토론 → 전략가의 헤지 비중 **제안**.

파이썬이 입력(`_market_inputs`)을 조립해 세션 1회(`kind="market"`)를 띄우고, 결과 `_market_view`
(HedgeProposal)를 디스크·`control.market_view:<market>` 에서 읽는다. 최종 결정은 `hedge.rebalance`
의 규칙이다 — 국면이 bear 가 아니면 제안이 RISK_OFF 여도 진입하지 않는다.
세션 상한·`hedge.market_session=false`·실패면 제안 없이(규칙 기본값) 간다.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trader.config import AccountConfig, Settings
from trader.orchestrator import run_store, tools
from trader.orchestrator import session as session_mod
from trader.orchestrator.run_store import checkpoint_payload as _wrap
from trader.orchestrator.run_store import read_checkpoint, write_checkpoint
from trader.risk import regime as regime_mod

logger = logging.getLogger("trader.market_view")

INDEX_CANDLES = 60
MACRO_NEWS_ITEMS = 10


def _f(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def build_inputs(
    conn: sqlite3.Connection,
    market: str,
    today: date,
    *,
    settings: Settings,
    asof: datetime,
    market_client: Any | None = None,
    secrets: Any | None = None,
    http: Any | None = None,
    accounts: list[AccountConfig] | None = None,
    brokers: dict[str, Any] | None = None,
    state: Path | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict:
    """시장 세션 입력. 실패한 절은 비우고 `errors` 에 남긴다(세션은 그래도 띄운다)."""
    errors: dict[str, str] = {}
    cfg = settings.hedge
    regime = regime_mod.load(conn, market) or {}
    sig = regime.get("signals") or {}
    inst = cfg.index_proxy.get(market)  # type: ignore[arg-type]
    index: dict[str, Any] = {"symbol": inst.symbol if inst else None, "closes": []}
    if inst is not None and market_client is not None:
        try:
            candles = (
                market_client.get_daily_us(inst.symbol, inst.exchange or "AMS", days=INDEX_CANDLES)
                if market == "US"
                else market_client.get_daily_kr(inst.symbol, days=INDEX_CANDLES)
            )
            cutoff = asof.date()
            rows = [c for c in candles if c.date <= cutoff]
            index["closes"] = [{"date": c.date.isoformat(), "close": str(c.close)} for c in rows]
            if rows:
                index["high_60"] = str(max(c.high for c in rows))
                index["low_60"] = str(min(c.low for c in rows))
        except Exception as e:  # noqa: BLE001 - 지수 봉은 보조 입력
            errors["index"] = f"{type(e).__name__}: {e}"
    macro: dict[str, Any] = {}
    try:
        from trader.config import load_sources
        from trader.sources.macro import collect_macro

        secs = (
            secrets
            if secrets is not None
            else type("S", (), {"FRED_API_KEY": None, "ECOS_API_KEY": None})()
        )
        macro = {
            k: v.model_dump() if hasattr(v, "model_dump") else v
            for k, v in collect_macro(
                load_sources().macro,
                secs,
                market_client=market_client,
                asof=asof,
                http=http,
                now=now,
            ).items()
        }
    except Exception as e:  # noqa: BLE001
        errors["macro"] = f"{type(e).__name__}: {e}"
    macro_news: list[dict] = []
    try:
        from trader.sources.base import owned_client
        from trader.sources.news import MACRO_QUERIES, GoogleNewsRss

        with owned_client(http, timeout=10) as client:
            items = GoogleNewsRss(client, now=now).search(
                MACRO_QUERIES[market],
                lang="ko" if market == "KR" else "en",
                max_items=MACRO_NEWS_ITEMS,  # type: ignore[index]
            )
        cutoff_iso = asof.astimezone(UTC).isoformat()
        macro_news = [
            i for i in items if not (i.get("published_at") and str(i["published_at"]) > cutoff_iso)
        ][:MACRO_NEWS_ITEMS]
    except Exception as e:  # noqa: BLE001
        errors["macro_news"] = f"{type(e).__name__}: {e}"
    notes: list[dict] = []
    try:
        from trader.sources import manual_inputs

        notes = manual_inputs.read_market(market, asof=asof, state=state)
    except Exception as e:  # noqa: BLE001
        errors["market_notes"] = f"{type(e).__name__}: {e}"
    held: list[str] = []
    sectors: list[dict] = []
    hedge_tickers = cfg.tickers(market)
    for a in accounts or []:
        if market not in a.markets:
            continue
        b = (brokers or {}).get(a.id)
        if b is None:
            continue
        try:
            for p in b.get_positions(market):
                if p.ticker in hedge_tickers and p.qty > 0 and p.ticker not in held:
                    held.append(p.ticker)
        except Exception as e:  # noqa: BLE001
            errors[f"positions.{a.id}"] = f"{type(e).__name__}: {e}"
        if a.env == "paper" and not sectors:
            try:
                from trader.config import load_sectors
                from trader.ledger import views

                snap = views.latest_positions(conn, a.id, market)
                if snap:
                    smap = load_sectors()

                    def _sector_of(t: str, _m=smap, _mk=market) -> str:
                        return _m.sector_of(t, _mk)  # type: ignore[arg-type]

                    sectors = [
                        {k: r[k] for k in ("sector", "weight", "count")}
                        for r in views.sector_weights(
                            snap["positions"],
                            snap["nav"],
                            _sector_of,
                            hedge_tickers=hedge_tickers,
                            cash=snap.get("cash"),
                        )
                    ]
            except Exception as e:  # noqa: BLE001
                errors["sector_weights"] = f"{type(e).__name__}: {e}"
    return {
        "market": market,
        "date": today.isoformat(),
        "asof": asof.isoformat(),
        "proxy": tools.MARKET_PROXY_LABEL.get(market, market),
        "regime": {
            "state": regime.get("state"),
            "count": regime.get("count"),
            "since": regime.get("since"),
            "signals": {k: sig.get(k) for k in ("ma_below", "momentum_weak", "vol_high")},
            "values": {
                k: _f(sig.get(k)) for k in ("close", "ma", "momentum", "realized_vol", "vix")
            },
        },
        "index": index,
        "macro": macro,
        "macro_news": macro_news,
        "market_notes": notes,
        "hedge": {
            "state": regime.get("state"),
            "max_weight_pct": cfg.max_weight_pct,
            "min_weight_pct": cfg.min_weight_pct,
            "leverage": cfg.leverage,
            "held": held,
        },
        "sector_weights": sectors,
        "errors": errors,
    }


def save_inputs(inputs: dict, *, market: str, today: date, state: Path | None) -> Path:
    d = run_store.market_dir(today, market, state, create=True)  # type: ignore[arg-type]
    return write_checkpoint(d, "_market_inputs", _wrap(inputs, None))


def run_market_view(
    settings: Settings,
    conn: sqlite3.Connection,
    accounts: list[AccountConfig],
    *,
    market: str,
    date: date,
    inputs: dict,
    cap_hit: bool,
    state: Path | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    brokers: dict[str, Any] | None = None,
    session_runner: Callable[..., Any] | None = None,
    cancel: Any | None = None,
) -> dict:
    """입력 저장 → 세션 1회 → `_market_view` 읽기. {view, session} 또는 {skipped}/{error}."""
    save_inputs(inputs, market=market, today=date, state=state)
    if not settings.hedge.enabled or not settings.hedge.market_session:
        return {"skipped": "hedge.market_session=false", "view": None}
    if cap_hit:
        return {"skipped": "세션 상한", "view": None}
    runner = session_runner or session_mod.run_session
    spec = session_mod.SessionSpec(
        market=market, date=date, tickers=[], dry_run=True, asof=now(), kind="market"
    )
    try:
        extra = {"cancel": cancel} if cancel is not None else {}
        res = runner(
            spec, settings, conn, accounts, state=state, now=now, brokers=brokers or {}, **extra
        )
    except session_mod.PipelineError as e:
        logger.warning("시장 세션 생략 %s: %s", market, e)
        return {"skipped": str(e), "view": None}
    view = (read_checkpoint(run_store.market_dir(date, market, state), "_market_view") or {}).get(  # type: ignore[arg-type]
        "data"
    )
    out: dict[str, Any] = {
        "view": view,
        "session": {"ok": res.ok, "fake": res.fake, "cost_usd": res.cost_usd, "error": res.error},
    }
    if not res.ok or view is None:
        logger.warning("시장 세션 실패 또는 제안 없음 %s: %s", market, res.error)
        out["error"] = res.error or "제안 없음"
    return out


def load_view(conn: sqlite3.Connection | None, market: str, today: date) -> dict | None:
    """오늘 제안(없거나 어제 것이면 None)."""
    return tools.market_view_today(conn, market, today)


def market_sessions_today(conn: sqlite3.Connection | None, now: datetime) -> int:
    """오늘(KST) 시작한 시장 세션 수 (`runs.kind='market'`, 실패 포함).
    `llm.market_session_cap` 용."""
    if conn is None:
        return 0
    from trader.config import KST
    from trader.ledger import repo

    start, _ = repo.day_bounds(now.astimezone(KST).date(), None)
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM runs WHERE kind='market' AND started_at >= ?", (start,)
    ).fetchone()
    return int(row["n"])


# --- 제안 적중률 복기 (원장 market_views, v13) ---------------------------------------

NEUTRAL_BAND = 0.02  # NEUTRAL 제안은 지수 수익이 ±2% 안이면 적중


def record_view(conn: sqlite3.Connection, *, today: date, market: str, view: dict) -> None:
    """제안을 원장에 남긴다(같은 날·시장은 덮어쓰되 채점 결과는 초기화). 적중률 복기의 원본."""
    conn.execute(
        "INSERT INTO market_views(date, market, stance, hedge_weight, leveraged, confidence, "
        "horizon_days, rationale, created_at) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(date, market) DO UPDATE SET stance=excluded.stance, "
        "hedge_weight=excluded.hedge_weight, leveraged=excluded.leveraged, "
        "confidence=excluded.confidence, horizon_days=excluded.horizon_days, "
        "rationale=excluded.rationale, created_at=excluded.created_at, "
        "matured_at=NULL, index_return=NULL, hit=NULL",
        (
            today.isoformat(),
            market,
            view.get("stance"),
            float(view.get("hedge_weight") or 0),
            1 if view.get("leveraged") else 0,
            float(view.get("confidence") or 0),
            int(view.get("horizon_days") or 20),
            str(view.get("rationale") or "")[:500],
            datetime.now(UTC).isoformat(),
        ),
    )
    conn.commit()


def is_hit(stance: str | None, ret: float) -> bool | None:
    if stance == "RISK_OFF":
        return ret < 0
    if stance == "RISK_ON":
        return ret > 0
    if stance == "NEUTRAL":
        return abs(ret) <= NEUTRAL_BAND
    return None


def mature_views(
    conn: sqlite3.Connection,
    market: str,
    *,
    today: date,
    candles: list[tuple[date, Decimal | float]],
) -> int:
    """만기(제안일 뒤 `horizon_days` 거래일) 가 지난 제안에 지수 proxy 수익과 적중 여부를 채운다.
    candles 는 (날짜, 종가) 오름차순. 만기 봉이 today 이하일 때만(미래를 보지 않음)."""
    rows = conn.execute(
        "SELECT id, date, stance, horizon_days FROM market_views "
        "WHERE market=? AND matured_at IS NULL ORDER BY date",
        (market,),
    ).fetchall()
    if not rows or not candles:
        return 0
    dates = [c[0] for c in candles]
    n = 0
    for r in rows:
        d0 = date.fromisoformat(r["date"])
        i0 = next((i for i, d in enumerate(dates) if d >= d0), None)
        if i0 is None:
            continue
        i1 = i0 + int(r["horizon_days"])
        if i1 >= len(candles) or dates[i1] > today:
            continue
        c0, c1 = float(candles[i0][1]), float(candles[i1][1])
        if c0 <= 0:
            continue
        ret = c1 / c0 - 1
        hit = is_hit(r["stance"], ret)
        conn.execute(
            "UPDATE market_views SET matured_at=?, index_return=?, hit=? WHERE id=?",
            (dates[i1].isoformat(), ret, None if hit is None else int(hit), r["id"]),
        )
        n += 1
    conn.commit()
    if n:
        logger.info("시장 제안 %d건 채점 %s", n, market)
    return n


def scorecard(conn: sqlite3.Connection, market: str) -> dict:
    """{n, matured, hits, hit_rate, by_stance{stance: {n, hits, avg_return}}}. 비어 있으면 n=0."""
    rows = conn.execute(
        "SELECT stance, index_return, hit, matured_at FROM market_views WHERE market=?",
        (market,),
    ).fetchall()
    by: dict[str, dict] = {}
    hits = matured = 0
    for r in rows:
        b = by.setdefault(r["stance"], {"n": 0, "matured": 0, "hits": 0, "sum_return": 0.0})
        b["n"] += 1
        if r["matured_at"] is not None:
            matured += 1
            b["matured"] += 1
            b["sum_return"] += float(r["index_return"] or 0)
            if r["hit"]:
                hits += 1
                b["hits"] += 1
    for b in by.values():
        b["avg_return"] = (b["sum_return"] / b["matured"]) if b["matured"] else None
        del b["sum_return"]
    return {
        "n": len(rows),
        "matured": matured,
        "hits": hits,
        "hit_rate": (hits / matured) if matured else None,
        "by_stance": by,
    }


def clamp_weight(view: dict | None, cfg) -> float:
    """제안 비중을 [min, max] 로 자른다. 제안이 없으면 max."""
    if not view or view.get("hedge_weight") is None:
        return float(cfg.max_weight_pct)
    w = float(Decimal(str(view["hedge_weight"])))
    return float(min(max(w, cfg.min_weight_pct), cfg.max_weight_pct))
