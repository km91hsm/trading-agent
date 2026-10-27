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
from trader.ml import dataset, features

TRADING_DAYS = 252
COST_BPS = (0, 10, 20, 30)


def nav_series(
    conn: sqlite3.Connection, account_id: str, market: str, *, days: int = 30, asset: str = "stock"
) -> list[tuple[str, Decimal]]:
    """계좌·시장의 (날짜, NAV). 파생 계좌(`asset="fo"`, Phase 26)는 `fo_nav_snapshot`(KR 만)."""
    if asset == "fo":
        return fo_nav_series(conn, account_id, days=days)
    return repo.nav_history(conn, account_id, market, days=days)


def fo_nav_series(
    conn: sqlite3.Connection, account_id: str, *, days: int = 30
) -> list[tuple[str, Decimal]]:
    last = conn.execute(
        "SELECT MAX(date) AS d FROM fo_nav_snapshot WHERE account_id=? AND nav IS NOT NULL",
        (account_id,),
    ).fetchone()["d"]
    if last is None:
        return []
    since = (date.fromisoformat(last) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT date, nav FROM fo_nav_snapshot WHERE account_id=? AND nav IS NOT NULL AND date > ? "
        "ORDER BY date",
        (account_id, since),
    ).fetchall()
    return [
        (r["date"], Decimal(str(r["nav"])))
        for r in rows
        if date.fromisoformat(r["date"]).weekday() < 5
    ]


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


def _avg_nav(
    conn, account_id: str, market: str, start: date, end: date, *, asset: str = "stock"
) -> Decimal:
    series = [
        v
        for d, v in nav_series(conn, account_id, market, days=10_000, asset=asset)
        if start.isoformat() <= d <= end.isoformat()
    ]
    return sum(series, Decimal(0)) / len(series) if series else Decimal(0)


def _traded_and_nav(conn, account_id: str, market: str, start: date, end: date, *, asset="stock"):
    # 파생 주문 금액은 계약 × 가격 × 승수로 저장돼 있어 같은 합산이 명목 거래대금이다(Phase 26)
    traded = _traded_amount(_orders_between(conn, account_id, market, start, end))
    return traded, _avg_nav(conn, account_id, market, start, end, asset=asset)


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


def _universe(market: str) -> list[str] | None:
    """퀀트 벤치마크 후보 = universe.yaml 의 그 시장 종목. 설정이 없으면 None(전체 읽기)."""
    try:
        from trader.config import load_universe

        u = load_universe()
    except Exception:  # noqa: BLE001 - 유니버스가 없으면 조기 종료 없이 읽는다
        return None
    return list(u.kr) if market == "KR" else [x.symbol for x in u.us]


def report(
    conn, state: Path | None, account_id: str, market: str, *, days: int = 30, asset: str = "stock"
) -> dict:
    series = nav_series(conn, account_id, market, days=days, asset=asset)
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

        cur = ranker.current(state, market=market)
        if cur is not None:
            # 종목별 최신 행은 latest_rows 로만, 유니버스를 넘겨 파일을 전부 읽지 않는다(이연 회수).
            # 선택은 **창 시작** 시점 피처로 — 창 끝 피처로 뽑으면 기간 수익을 미리 본 벤치마크다
            latest = features.latest_rows(state, market, tickers=_universe(market), asof=start)
            if not latest.empty:
                ranked = cur[0](latest.reset_index(drop=True))
                quant_top = list(ranked["ticker"].head(5))
    except Exception:  # noqa: BLE001 - 퀀트 벤치마크는 선택
        quant_top = None
    now = datetime.now(UTC)
    traded, avg_nav = (
        _traded_and_nav(conn, account_id, market, start, end, asset=asset)
        if series
        else (Decimal(0), Decimal(0))
    )
    return {
        "account_id": account_id,
        "market": market,
        "days": days,
        "nav": [{"time": d, "value": str(v)} for d, v in series],
        "metrics": m,
        "benchmarks": benchmarks(
            state, market, start, end, tickers=held or None, quant_top=quant_top
        ),
        "cost_sensitivity": _cost_sensitivity_of(traded, avg_nav) if series else {},
        "turnover": _turnover_of(traded, avg_nav) if series else 0.0,
        "decision_distribution": ledger_metrics.decision_distribution(
            conn, days=days, now=now, market=market
        ),
        "convergence_rate": ledger_metrics.convergence_rate(conn, days=days, now=now),
    }
