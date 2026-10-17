"""성과 지표 (스펙 §13): NAV·수익률·MDD·Sortino·벤치마크(1/N·B&H·퀀트)·비용 민감도·회전율.

NAV 는 positions_snapshot 일별 최신, 벤치마크는 피처 스토어 종가. 금액 Decimal, 비율 float.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd

from trader.ledger import metrics as ledger_metrics
from trader.ledger import repo
from trader.ml import dataset

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


metrics = metrics_of  # noqa: F811 - 모듈 공개 이름 (테스트·라우터가 performance.metrics 로 부른다)


def _close_table(state: Path | None, market: str, start: date, end: date) -> pd.DataFrame:
    df = dataset.load_all(state, market=market)
    if df.empty:
        return pd.DataFrame()
    df = df[["date", "ticker", "close"]].copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df[(df["date"] >= start) & (df["date"] <= end)]
    if df.empty:
        return pd.DataFrame()
    return df.pivot_table(
        index="date", columns="ticker", values="close", aggfunc="last"
    ).sort_index()


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
    """1/N(등가중 일별 리밸런스)·B&H(시작일 등가중 보유)·quant(승격 모델 상위, 없으면 None).

    각 항목 {return, series: [{time, value(시작=1.0)}]}. 종가가 없으면 return None·series [].
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

    cols = tickers if tickers else (list(table.columns) if not table.empty else [])
    return {
        "one_over_n": strategy(cols, rebalance=True),
        "buy_and_hold": strategy(cols, rebalance=False),
        "quant": strategy(quant_top, rebalance=True) if quant_top else None,
    }


def _orders_between(
    conn, account_id: str, market: str, start: date, end: date
) -> list[sqlite3.Row]:
    lo = f"{start.isoformat()}T00:00:00"
    hi = f"{(end + timedelta(days=1)).isoformat()}T00:00:00"
    return conn.execute(
        "SELECT qty, price, amount, side FROM orders WHERE account_id=? AND market=? "
        "AND status NOT IN ('rejected','cancelled','failed') "
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


def cost_sensitivity(
    conn, account_id: str, market: str, start: date, end: date, bps=COST_BPS
) -> dict[str, float]:
    """거래금액 × bp 를 평균 NAV 대비 비율로 뺀 순효과 {bp: −비용률}."""
    traded = _traded_amount(_orders_between(conn, account_id, market, start, end))
    nav = _avg_nav(conn, account_id, market, start, end)
    out = {}
    for bp in bps:
        cost = traded * Decimal(bp) / Decimal(10_000)
        out[str(bp)] = round(float(-cost / nav), 6) if nav > 0 else 0.0
    return out


def turnover(conn, account_id: str, market: str, start: date, end: date) -> float:
    traded = _traded_amount(_orders_between(conn, account_id, market, start, end))
    nav = _avg_nav(conn, account_id, market, start, end)
    return round(float(traded / nav), 6) if nav > 0 else 0.0


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
    quant_top = None
    try:
        from trader.ml import ranker

        cur = ranker.current(state)
        if cur is not None:
            latest = dataset.load_all(state, market=market)
            if not latest.empty:
                latest = latest.sort_values("date").groupby("ticker", as_index=False).tail(1)
                ranked = cur[0](latest.reset_index(drop=True))
                quant_top = list(ranked["ticker"].head(5))
    except Exception:  # noqa: BLE001 - 퀀트 벤치마크는 선택
        quant_top = None
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return {
        "account_id": account_id,
        "market": market,
        "days": days,
        "nav": [{"time": d, "value": str(v)} for d, v in series],
        "metrics": m,
        "benchmarks": benchmarks(
            state, market, start, end, tickers=held or None, quant_top=quant_top
        ),
        "cost_sensitivity": cost_sensitivity(conn, account_id, market, start, end)
        if series
        else {},
        "turnover": turnover(conn, account_id, market, start, end) if series else 0.0,
        "decision_distribution": ledger_metrics.decision_distribution(
            conn, days=days, now=now, market=market
        ),
        "convergence_rate": ledger_metrics.convergence_rate(conn, days=days, now=now),
    }
