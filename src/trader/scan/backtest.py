"""정량 스크린 과거 검증(10-26 검토 L15) — 피처 스토어만으로, 네트워크 없이.

scan 의 정량 필터·점수(`scan/quant.score`)는 근거가 저장소에 없었다(리뷰어 2인 공통 지적). 여기서는
같은 날의 피처 행(완성 일봉)으로 세 바구니를 고르고 **다음 거래일 종가에 들어가 `horizon` 거래일 뒤
종가에 나오는** 수익을 비교한다.

- `momentum`: 정량 필터를 통과한 종목 중 `roc_10` 상위 N(단순 모멘텀 기준선)
- `screen`: 실제 scan 점수(`quant.score`) 상위 N
- `universe_1n`: 정량 필터를 통과한 종목 전체 균등(대조군)

창은 `horizon` 간격으로 겹치지 않게 잡고(겹치는 창은 표본 수를 부풀린다), 세 바구니 모두 같은 왕복
비용을 뺀다. 진입·청산 종가가 없는 종목은 채우지 않고 빼며 `missing` 으로 센다. 피처 스토어는 오늘의
유니버스로 백필됐으므로 시점 유니버스 기록(`ml/universe_pit`) 이전 구간에는 생존 편향이 남는다 —
CLI 가 그 범위(`pit`)를 함께 보여 준다. 설정·주문과 무관한 읽기 전용 리포트다.
"""

from __future__ import annotations

import math

import pandas as pd

from trader.scan import quant

STRATEGIES = ("momentum", "screen", "universe_1n")


def _basket_return(
    closes: pd.DataFrame, tickers: list[str], entry, exit_, cost: float
) -> tuple[float | None, int]:
    """(균등 평균 수익 − 왕복비용, 가격 없는 종목 수)."""
    if not tickers:
        return None, 0
    try:
        p0 = closes.loc[entry, tickers]
        p1 = closes.loc[exit_, tickers]
    except KeyError:
        return None, len(tickers)
    r = (p1 / p0 - 1).replace([float("inf"), float("-inf")], float("nan")).dropna()
    missing = len(tickers) - len(r)
    if r.empty:
        return None, missing
    return float(r.mean()) - cost, missing


def _summary(vals: list[float], base: list[float]) -> dict:
    n = len(vals)
    if not n:
        return {"windows": 0, "mean": None, "median": None, "excess_mean": None, "t_excess": None}
    ex = [v - b for v, b in zip(vals, base, strict=True)]
    mean_ex = sum(ex) / n
    sd = (sum((x - mean_ex) ** 2 for x in ex) / (n - 1)) ** 0.5 if n > 1 else 0.0
    return {
        "windows": n,
        "mean": round(sum(vals) / n, 6),
        "median": round(float(pd.Series(vals).median()), 6),
        "excess_mean": round(mean_ex, 6),  # 같은 창의 universe_1n 대비
        "excess_hit": round(sum(x > 0 for x in ex) / n, 4),
        "t_excess": round(mean_ex / (sd / math.sqrt(n)), 3) if sd > 0 else None,
    }


def run(
    df: pd.DataFrame,
    *,
    market: str,
    top_n: int = 5,
    horizon: int = 5,
    cost_bp: float,
) -> dict:
    """df: 피처 행(date·ticker·close·volume·roc_10·high_52w_pos·atr_14). 순수 함수."""
    out: dict = {
        "market": market,
        "top_n": top_n,
        "horizon": horizon,
        "cost_bp": cost_bp,
        "overlapping": False,
        "windows": [],
        "missing_total": 0,
    }
    if df is None or df.empty:
        out["summary"] = {k: _summary([], []) for k in STRATEGIES}
        return out
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"])
    closes = d.pivot_table(index="date", columns="ticker", values="close", aggfunc="last")
    dates = list(closes.index)
    cost = float(cost_bp) / 10_000
    vals: dict[str, list[float]] = {k: [] for k in STRATEGIES}
    for i in range(0, len(dates) - horizon - 1, horizon):
        signal, entry, exit_ = dates[i], dates[i + 1], dates[i + 1 + horizon]
        rows = d[d["date"] == signal]
        ranked = quant.score(rows, top=len(rows))  # 필터·결측 제외를 scan 과 같이
        if ranked.empty:
            continue
        passing = list(ranked["ticker"])
        mom = (
            rows.set_index("ticker")
            .loc[passing, "roc_10"]
            .astype(float)
            .sort_values(ascending=False, kind="stable")
        )
        picks = {
            "momentum": list(mom.index[:top_n]),
            "screen": passing[:top_n],
            "universe_1n": passing,
        }
        rets: dict[str, float | None] = {}
        missing = 0
        for k, tickers in picks.items():
            rets[k], miss = _basket_return(closes, tickers, entry, exit_, cost)
            if k != "universe_1n":
                missing += miss
        if any(v is None for v in rets.values()):
            continue
        for k in STRATEGIES:
            vals[k].append(rets[k])  # type: ignore[arg-type]
        out["missing_total"] += missing
        out["windows"].append(
            {
                "signal_date": signal.date().isoformat(),
                "entry": entry.date().isoformat(),
                "exit": exit_.date().isoformat(),
                "n_universe": len(passing),
                "missing": missing,
                **{k: round(v, 6) for k, v in rets.items() if v is not None},
            }
        )
    base = vals["universe_1n"]
    out["summary"] = {k: _summary(vals[k], base) for k in STRATEGIES}
    return out
