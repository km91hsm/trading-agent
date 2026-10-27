"""포트폴리오 위험 카드(Stock D-088 교훈): 종목 5개 집중이라 숨은 상관이 위험을 키운다 — 지금 보유
비중에 최근 `WINDOW` 거래일 수익을 적용해 하루 VaR·CVaR 95%(손실 비율), 유니버스 1/N 대비 베타,
보유 종목 상관, 종목별 위험 기여(분산 기여, 합 1)를 낸다. 종가는 피처 스토어. 표시 전용 — 게이트·
주문은 읽지 않는다. `GET /api/portfolio/risk`."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from trader.ledger import views

WINDOW = 60
MIN_OBS = 20
CACHE_TTL_S = 300.0
# (account, market, today, window) → (version, saved_at, card). 계좌 화면은 SSE 마다 다시
# 읽는데 종가표 계산(parquet 100여 개)이 1초를 넘는다. 입력(스냅샷·피처 스토어)이 같으면 결과도 같다
_CACHE: dict[tuple, tuple[tuple, float, dict]] = {}
_LOCK = threading.Lock()


def _version(conn: sqlite3.Connection, account_id: str, market: str, state: Path | None) -> tuple:
    from trader.ml import features

    # MAX(created_at): 같은 날 같은 종목의 upsert(평가액·NAV 변경)는 행 수·id 를 바꾸지 않지만
    # created_at 은 매번 갱신된다 — 값이 바뀐 잔고를 낡은 카드로 돌려주지 않게(Codex 리뷰)
    snap = conn.execute(
        "SELECT COUNT(*), MAX(id), MAX(created_at) FROM positions_snapshot "
        "WHERE account_id=? AND market=?",
        (account_id, market),
    ).fetchone()
    return (snap[0], snap[1], snap[2], features.store_mtime(state))


def cached_compute(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    market: str,
    today: date,
    state: Path | None = None,
    window: int = WINDOW,
) -> dict:
    """`compute` 결과를 (계좌·시장·기준일·창) 별로 기억한다. 스냅샷 행(개수·최대 id)이나 피처
    디렉터리 mtime 이 바뀌면, 또는 `CACHE_TTL_S` 가 지나면 다시 계산한다(웹 위험 카드용)."""
    key = (account_id, market, today, window, str(state))
    version = _version(conn, account_id, market, state)
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None and hit[0] == version and now - hit[1] < CACHE_TTL_S:
            return hit[2]
    card = compute(
        conn, account_id=account_id, market=market, today=today, state=state, window=window
    )
    with _LOCK:
        for k in [k for k, (_, saved, _) in _CACHE.items() if now - saved >= CACHE_TTL_S]:
            del _CACHE[k]
        _CACHE[key] = (version, now, card)
    return card


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
    table = dataset.close_table(
        state, market, since=today - timedelta(days=window * 2 + 30), until=today
    )
    missing = sorted(t for t in held if t not in table.columns)
    rets = table.pct_change(fill_method=None).iloc[1:].tail(window) if not table.empty else table
    # 보유 종목별 유효 수익률이 MIN_OBS 미만이면 '종가 부족' — 0 으로 채우면 위험이 사라진다(Codex)
    thin = [t for t in held if t not in missing and int(rets[t].notna().sum()) < MIN_OBS]
    missing = sorted(set(missing) | set(thin))
    have = [t for t in held if t not in missing]
    sub = rets[have].dropna() if have else rets.iloc[0:0]
    if not have or len(sub) < MIN_OBS:
        return {"insufficient": f"종가 부족({len(sub)}일)", "missing": missing}
    rets = rets.loc[sub.index]
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
        # 빠진 보유 종목이 있으면 그 비중만큼 위험을 덜 본 부분 추정
        "partial": bool(missing),
        "snapshot_date": snap["date"],
    }
