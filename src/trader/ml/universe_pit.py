"""시점 기준 유니버스(Stock D-037 교훈): Stock 은 '지금 시총 상위 300' 으로 고른 과거의 연 +6% 가
선택 편향이었음을 재측정에서 확인했다. 이 저장소의 피처 스토어도 오늘의 `universe.yaml`(KOSPI 200
등)로 3년을 백필하므로 그 사이 빠진·상장폐지 종목이 없다.

과거 구성원 목록은 무료로 얻을 길이 없어 **오늘부터 기록**한다: 데몬이 매일 그날의 유니버스
(+ 보유)를 `universe_members`(시장·종목 → first_seen·last_seen)에 남기고, 학습
(`ml/train.prepare_dataset`)은 기록이 시작된 날 이후 행을 **그날 구성원이었던 종목만** 쓴다. 편출된
종목은 기록이 멈춘 날까지의 행이 남아(피처 스토어는 지우지 않는다) 생존 편향이 생기지 않는다. 기록
전 날짜는 `strict` 가 꺼져 있으면 그대로 쓰고 그 비율(`coverage`)을 학습 결과에 남긴다 — 기록이
쌓이면 `strict` 로 바꾼다. `liquidity_top_n` 은 그날까지의 거래대금으로만 고른다.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from datetime import date, timedelta

import pandas as pd

from trader.config import PitCfg

logger = logging.getLogger("trader.ml")

GRACE = timedelta(days=5)  # 기록이 하루이틀 빠져도(휴장·데몬 정지) 구성원으로 본다


def record(
    conn: sqlite3.Connection,
    *,
    market: str,
    tickers: Iterable[str],
    day: date,
    source: str = "universe",
) -> int:
    """그날 구성원을 남긴다(멱등): 새 종목은 first_seen=day, 있던 종목은 last_seen 을 늘린다."""
    n = 0
    d = day.isoformat()
    for t in sorted({str(x) for x in tickers if x}):
        conn.execute(
            "INSERT INTO universe_members(market, ticker, first_seen, last_seen, source) "
            "VALUES (?,?,?,?,?) ON CONFLICT(market, ticker) DO UPDATE SET "
            "first_seen=min(first_seen, excluded.first_seen), "
            "last_seen=max(last_seen, excluded.last_seen)",
            (market, t, d, d, source),
        )
        n += 1
    conn.commit()
    return n


def members(conn: sqlite3.Connection, market: str) -> pd.DataFrame:
    rows = conn.execute(
        "SELECT ticker, first_seen, last_seen FROM universe_members WHERE market=?", (market,)
    ).fetchall()
    return pd.DataFrame(
        [(r[0], date.fromisoformat(r[1]), date.fromisoformat(r[2])) for r in rows],
        columns=["ticker", "first_seen", "last_seen"],
    )


def _liquidity_mask(df: pd.DataFrame, top_n: int, window: int) -> pd.Series:
    """그날까지의 window 일 평균 거래대금(종가×거래량) 순위가 (날짜·시장) 안 top_n 이하인 행."""
    turnover = (df["close"].astype(float) * df["volume"].astype(float)).fillna(0.0)
    trailing = turnover.groupby(df["ticker"]).transform(
        lambda s: s.rolling(window, min_periods=1).mean()
    )
    rank = trailing.groupby([df["date"], df["market"]]).rank(ascending=False, method="first")
    return rank <= top_n


def apply(
    df: pd.DataFrame, conn: sqlite3.Connection | None, cfg: PitCfg
) -> tuple[pd.DataFrame, dict]:
    """(걸러진 df, 정보). df 는 date(datetime64)·ticker·market 열을 가진 피처 행(정렬 무관)."""
    info: dict = {"strict": cfg.strict, "liquidity_top_n": cfg.liquidity_top_n, "markets": {}}
    if df.empty:
        return df, info
    total = len(df)
    keep = pd.Series(True, index=df.index)
    days = df["date"].dt.date
    for market in sorted(df["market"].dropna().unique()):
        in_m = df["market"] == market
        mem = members(conn, str(market)) if conn is not None else pd.DataFrame()
        if mem.empty:
            info["markets"][market] = {"recorded_since": None, "coverage": 0.0}
            if cfg.strict:
                keep &= ~in_m
            continue
        start = min(mem["first_seen"])
        recorded = in_m & (days >= start)
        span = mem.set_index("ticker")
        first = df["ticker"].map(span["first_seen"])
        last = df["ticker"].map(span["last_seen"])
        member = (
            first.notna()
            & (days >= first.fillna(date.max))
            & (days <= last.map(lambda x: x + GRACE if pd.notna(x) else date.min))
        )
        keep &= ~recorded | member
        if cfg.strict:
            keep &= ~in_m | recorded
        n_m = int(in_m.sum())
        info["markets"][market] = {
            "recorded_since": start.isoformat(),
            "coverage": round(int(recorded.sum()) / n_m, 4) if n_m else 0.0,
            "dropped_non_member": int((recorded & ~member).sum()),
        }
    out = df[keep]
    if cfg.liquidity_top_n > 0 and not out.empty:
        out = out[_liquidity_mask(out, cfg.liquidity_top_n, cfg.liquidity_window)]
    info["rows_before"], info["rows_after"] = total, len(out)
    covered = [v["coverage"] for v in info["markets"].values()]
    info["coverage"] = round(min(covered), 4) if covered else 0.0
    return out.reset_index(drop=True), info
