"""포트폴리오 위험 카드(Stock D-088 교훈): 종목 5개 집중이라 숨은 상관이 위험을 키운다 — 지금 보유
비중에 최근 `WINDOW` 거래일 수익을 적용해 하루 VaR·CVaR 95%(손실 비율), 유니버스 1/N 대비 베타,
보유 종목 상관, 종목별 위험 기여(분산 기여, 합 1)를 낸다. 종가는 피처 스토어. 표시 전용 — 게이트·
주문은 읽지 않는다. `GET /api/portfolio/risk`."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from trader.ledger import views

WINDOW = 60
MIN_OBS = 20


def compute(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    market: str,
    today: date,
    state: Path | None = None,
    window: int = WINDOW,
) -> dict:
    from trader.ml import dataset

    snap = views.latest_positions(conn, account_id, market)
    if not snap:
        return {"insufficient": "잔고 스냅샷 없음"}
    nav = float(snap["nav"] or 0)
    held = {
        str(p["ticker"]): float(p["eval_amount"] or 0) / nav
        for p in snap["positions"]
        if nav > 0 and float(p["eval_amount"] or 0) > 0
    }
    if not held:
        return {"insufficient": "보유 없음(전액 현금)"}
    df = dataset.load_all(state, market=market, since=today - timedelta(days=window * 2 + 30))
    table = (
        df.pivot_table(index="date", columns="ticker", values="close", aggfunc="last").sort_index()
        if not df.empty
        else pd.DataFrame()
    )
    missing = sorted(t for t in held if t not in table.columns)
    rets = table.pct_change(fill_method=None).iloc[1:].tail(window) if not table.empty else table
    have = [t for t in held if t not in missing]
    if not have or len(rets) < MIN_OBS:
        return {"insufficient": f"종가 부족({len(rets)}일)", "missing": missing}
    sub = rets[have].fillna(0.0)
    w = np.array([held[t] for t in have])
    port = sub.to_numpy() @ w
    q = float(np.quantile(port, 0.05))
    tail = port[port <= q]
    mkt = rets.mean(axis=1, skipna=True).fillna(0.0).to_numpy()  # 유니버스 1/N(지수 대용)
    var_m = float(np.var(mkt, ddof=1))
    beta = float(np.cov(port, mkt, ddof=1)[0, 1] / var_m) if var_m > 0 else None
    var_p = float(np.var(port, ddof=1))
    contributions = []
    for i, t in enumerate(have):
        cov = float(np.cov(sub[t].to_numpy(), port, ddof=1)[0, 1])
        contributions.append(
            {
                "ticker": t,
                "weight": round(held[t], 6),
                "contribution": w[i] * cov / var_p if var_p > 0 else 0.0,
            }
        )
    corr = sub.corr().fillna(0.0) if len(have) >= 2 else pd.DataFrame()
    return {
        "asof": pd.Timestamp(rets.index[-1]).date().isoformat(),
        "window": window,
        "n_obs": int(len(rets)),
        "var95": -q,
        "cvar95": -float(tail.mean()) if len(tail) else -q,
        "beta": beta,
        "corr": {
            "tickers": list(corr.columns) if not corr.empty else have,
            "matrix": corr.round(4).to_numpy().tolist() if not corr.empty else [],
        },
        "contributions": sorted(contributions, key=lambda x: -x["contribution"]),
        "missing": missing,
        "snapshot_date": snap["date"],
    }
