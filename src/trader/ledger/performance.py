"""성과 지표 (스펙 §13): NAV·수익률·MDD·Sortino·벤치마크(1/N·B&H·퀀트)·비용 민감도·회전율.

NAV 는 positions_snapshot 일별 최신, 벤치마크는 피처 스토어 종가. 금액 Decimal, 비율 float.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd

from trader.ledger import metrics as ledger_metrics
from trader.ledger import repo
from trader.ml import dataset
from trader.risk import costs

TRADING_DAYS = 252
COST_BPS = (0, 10, 20, 30)


def nav_series(
    conn: sqlite3.Connection, account_id: str, market: str, *, days: int = 30
) -> list[tuple[str, Decimal]]:
    return repo.nav_history(conn, account_id, market, days=days)


def _returns(values: list[Decimal]) -> list[float]:
    out = []
    for a, b in zip(values, values[1:], strict=False):
        out.append(float(b / a - 1) if a else 0.0)
    return out


def metrics_of(series: list[tuple[str, Decimal]]) -> dict:
    """{return, mdd, sortino, days, start, end}. 2개 미만이면 지표 None."""
    if not series:
        return {"return": None, "mdd": None, "sortino": None, "days": 0, "start": None, "end": None}
    values = [Decimal(str(v)) for _, v in series]
    out = {"days": len(series), "start": series[0][0], "end": series[-1][0]}
    if len(values) < 2 or values[0] <= 0:
        return {"return": None, "mdd": None, "sortino": None, **out}
    total = float(values[-1] / values[0] - 1)
    peak = values[0]
    mdd = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, float(v / peak - 1))
    rets = _returns(values)
    downside = [min(r, 0.0) for r in rets]
    dd = math.sqrt(sum(d * d for d in downside) / len(downside)) if downside else 0.0
    mean = sum(rets) / len(rets) if rets else 0.0
    sortino = (mean / dd) * math.sqrt(TRADING_DAYS) if dd > 0 else None
    return {
        "return": round(total, 6),
        "mdd": round(mdd, 6),
        "sortino": None if sortino is None else round(sortino, 4),
        **out,
    }


def _close_table(state: Path | None, market: str, start: date, end: date) -> pd.DataFrame:
    # 창 안의 날짜 파일·close 열만 읽는다 (성과 페이지는 원장 이벤트마다 다시 불린다)
    return dataset.close_table(state, market, since=start, until=end)


def _series_from_growth(growth: pd.Series) -> list[dict]:
    return [{"time": d.isoformat(), "value": round(float(v), 6)} for d, v in growth.items()]


def benchmarks(
    state: Path | None,
    market: str,
    start: date,
    end: date,
    *,
    tickers: list[str] | None = None,
    quant_top: list[str] | None = None,
) -> dict:
    """1/N(유니버스 등가중 일별 리밸런스)·B&H(tickers 또는 유니버스 시작일 등가중 보유)·quant.

    각 항목 {return, series: [{time, value(시작=1.0)}]}. 종가가 없으면 return None·series [].
    benchmark_universe 는 1/N 에 들어간 종목 목록이다.
    """
    table = _close_table(state, market, start, end)
    empty = {"return": None, "series": []}

    def strategy(cols: list[str], rebalance: bool) -> dict:
        sub = (
            table[[c for c in cols if c in table.columns]].dropna(how="all")
            if not table.empty
            else table
        )
        if sub.empty or len(sub) < 2:
            return dict(empty)
        sub = sub.ffill().dropna(axis=1, how="any")
        if sub.empty:
            return dict(empty)
        if rebalance:
            daily = (sub / sub.shift(1) - 1).mean(axis=1).fillna(0.0)
            growth = (1 + daily).cumprod()
        else:
            growth = (sub / sub.iloc[0]).mean(axis=1)
        return {
            "return": round(float(growth.iloc[-1] - 1), 6),
            "series": _series_from_growth(growth),
        }

    universe = [str(c) for c in table.columns] if not table.empty else []
    return {
        "one_over_n": strategy(universe, rebalance=True),
        "buy_and_hold": strategy(tickers or universe, rebalance=False),
        "quant": strategy(quant_top, rebalance=True) if quant_top else None,
        "benchmark_universe": universe,
    }


def shadow_quant_benchmark(
    conn, state: Path | None, market: str, start: date, end: date, *, cost_bp: float
) -> dict | None:
    """기록된 섀도 `quant_topn` 바구니를 따른 기준선(L14). 운영일 d 의 바구니를 d 종가에 사서 다음
    기록까지 **그대로 들고**(비중은 가격 따라 표류), 새 기록이 오면 표류한 비중에서 목표로
    리밸런스하며 바뀐 비중만큼 왕복비용(`0.5 × Σ|Δw| × 왕복`)을 뺀다(PR #63 Codex — 매일 목표
    비중으로 되돌리면 숨은 리밸런스 수익과 그 비용이 빠졌다). 종가가 없는 날 그 종목은 수익 0 으로
    본다. 첫 기록 전은 없음(N/A) — 기록이 없으면 None. {return, series, source, since}."""
    import json as _json

    rows = conn.execute(
        "SELECT date, weights_json FROM shadow_portfolios WHERE market=? AND strategy='quant_topn' "
        "AND date <= ? ORDER BY date",
        (market, end.isoformat()),
    ).fetchall()
    if not rows:
        return None
    baskets = [
        (date.fromisoformat(r["date"]), _json.loads(r["weights_json"] or "{}")) for r in rows
    ]
    first = max(baskets[0][0], start)
    table = _close_table(state, market, first - timedelta(days=10), end)
    if table.empty:
        return None
    days = [d for d in table.index if first <= d <= end]
    if len(days) < 2:
        return None
    cost = float(cost_bp) / 10_000
    held: dict[str, float] = {}  # 표류한 비중(합 ≤ 1, 나머지는 현금)
    applied: date | None = None
    growth = 1.0
    points = [(days[0], 1.0)]
    for prev, cur in zip(days, days[1:], strict=False):
        recs = [(d, w) for d, w in baskets if d <= prev]
        if recs and recs[-1][0] != applied:  # 새 기록이 왔을 때만 리밸런스
            applied, target = recs[-1][0], recs[-1][1]
            turnover = 0.5 * sum(
                abs(target.get(t, 0.0) - held.get(t, 0.0)) for t in set(target) | set(held)
            )
            growth *= 1 - turnover * cost
            held = dict(target)
        rets: dict[str, float] = {}
        for t in held:
            r = 0.0
            if t in table.columns:
                p0, p1 = table.at[prev, t], table.at[cur, t]
                if pd.notna(p0) and pd.notna(p1) and p0 > 0:
                    r = float(p1) / float(p0) - 1
            rets[t] = r
        port = sum(w * rets[t] for t, w in held.items())
        growth *= 1 + port
        if 1 + port > 0:
            held = {t: w * (1 + rets[t]) / (1 + port) for t, w in held.items()}
        points.append((cur, growth))
    return {
        "return": round(growth - 1, 6),
        "series": [{"time": d.isoformat(), "value": round(v, 6)} for d, v in points],
        "source": "shadow_quant_topn",
        "since": baskets[0][0].isoformat(),
    }


def _orders_between(
    conn, account_id: str, market: str, start: date, end: date
) -> list[sqlite3.Row]:
    # 주문 created_at 은 UTC 저장이라 KST 자정 경계를 UTC 로 바꿔 비교한다 (repo.day_bounds)
    lo, _ = repo.day_bounds(start, None)
    _, hi = repo.day_bounds(end, None)
    return conn.execute(
        "SELECT qty, price, amount, side FROM orders WHERE account_id=? AND market=? "
        f"AND status NOT IN ({repo.dead_status_sql()}) "
        "AND created_at >= ? AND created_at < ?",
        (account_id, market, lo, hi),
    ).fetchall()


def _traded_amount(rows) -> Decimal:
    total = Decimal(0)
    for r in rows:
        if r["amount"] is not None:
            total += Decimal(str(r["amount"]))
        elif r["price"] is not None:
            total += Decimal(r["qty"]) * Decimal(str(r["price"]))
    return total


def _avg_nav(conn, account_id: str, market: str, start: date, end: date) -> Decimal:
    series = [
        v
        for d, v in repo.nav_history(conn, account_id, market, days=10_000)
        if start.isoformat() <= d <= end.isoformat()
    ]
    return sum(series, Decimal(0)) / len(series) if series else Decimal(0)


def _traded_and_nav(conn, account_id: str, market: str, start: date, end: date):
    traded = _traded_amount(_orders_between(conn, account_id, market, start, end))
    return traded, _avg_nav(conn, account_id, market, start, end)


def _cost_sensitivity_of(traded: Decimal, nav: Decimal, bps=COST_BPS) -> dict[str, float]:
    out = {}
    for bp in bps:
        cost = traded * Decimal(bp) / Decimal(10_000)
        out[str(bp)] = round(float(-cost / nav), 6) if nav > 0 else 0.0
    return out


def _turnover_of(traded: Decimal, nav: Decimal) -> float:
    return round(float(traded / nav), 6) if nav > 0 else 0.0


def cost_sensitivity(
    conn, account_id: str, market: str, start: date, end: date, bps=COST_BPS
) -> dict[str, float]:
    """거래금액 × bp 를 평균 NAV 대비 비율로 뺀 순효과 {bp: −비용률}."""
    traded, nav = _traded_and_nav(conn, account_id, market, start, end)
    return _cost_sensitivity_of(traded, nav, bps)


def turnover(conn, account_id: str, market: str, start: date, end: date) -> float:
    traded, nav = _traded_and_nav(conn, account_id, market, start, end)
    return _turnover_of(traded, nav)


def report(conn, state: Path | None, account_id: str, market: str, *, days: int = 30) -> dict:
    series = nav_series(conn, account_id, market, days=days)
    m = metrics_of(series)
    end = date.fromisoformat(series[-1][0]) if series else date.today()
    start = date.fromisoformat(series[0][0]) if series else end - timedelta(days=days)
    held = sorted(
        {
            r["ticker"]
            for r in conn.execute(
                "SELECT DISTINCT ticker FROM positions_snapshot WHERE account_id=? AND market=? "
                "AND ticker NOT LIKE '\\_CASH%' ESCAPE '\\'",
                (account_id, market),
            ).fetchall()
        }
    )
    # 퀀트 기준선(10-26 검토 L14): 현재 승격 모델을 과거 창에 소급 적용하면 그 창 뒤에 학습한 모델이
    # 과거를 고른다(룩어헤드) — 그날 기록된 섀도 `quant_topn`(당시 정량 스크린)을 따른다
    try:
        quant = shadow_quant_benchmark(
            conn, state, market, start, end, cost_bp=costs.round_trip_bp(market)
        )
    except Exception:  # noqa: BLE001 - 퀀트 벤치마크는 선택
        quant = None
    now = datetime.now(UTC)
    traded, avg_nav = (
        _traded_and_nav(conn, account_id, market, start, end)
        if series
        else (Decimal(0), Decimal(0))
    )
    return {
        "account_id": account_id,
        "market": market,
        "days": days,
        "nav": [{"time": d, "value": str(v)} for d, v in series],
        "metrics": m,
        "benchmarks": {
            **benchmarks(state, market, start, end, tickers=held or None),
            "quant": quant,
        },
        "cost_sensitivity": _cost_sensitivity_of(traded, avg_nav) if series else {},
        "turnover": _turnover_of(traded, avg_nav) if series else 0.0,
        "decision_distribution": ledger_metrics.decision_distribution(
            conn, days=days, now=now, market=market
        ),
        "convergence_rate": ledger_metrics.convergence_rate(conn, days=days, now=now),
    }
