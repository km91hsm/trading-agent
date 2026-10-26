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
from datetime import date, datetime, timedelta

import pandas as pd

from trader.config import PitCfg

logger = logging.getLogger("trader.ml")

GRACE = timedelta(days=5)  # 기록이 하루이틀 빠져도(휴장·데몬 정지) 구성원으로 본다


# 기록 사이가 유예(GRACE)보다 벌어지면 새 구간 — 병합이 유예 밖 날짜를 소급해 구성원으로 만들지 않게
MERGE_GAP = GRACE


def effective_day(market: str, now: datetime) -> date:
    """구성 관측의 효력 거래일: 그 시장 정규장이 이미 끝난 뒤의 관측은 다음 날부터(장 마감 뒤 새로
    편입한 종목의 그날 행을 당시 구성원으로 소급하지 않는다)."""
    from trader.config import MARKET_TZ
    from trader.market_hours import REGULAR

    local = now.astimezone(MARKET_TZ[market])
    close_h, close_m = (int(x) for x in REGULAR[market][1].split(":"))
    after = (local.hour, local.minute) >= (close_h, close_m)
    return local.date() + timedelta(days=1) if after else local.date()


def record(
    conn: sqlite3.Connection,
    *,
    market: str,
    tickers: Iterable[str],
    day: date,
    source: str = "universe",
) -> int:
    """그날 구성원을 남긴다(멱등): 마지막 구간과 MERGE_GAP 안이면 그 구간을 늘리고, 아니면(편출 뒤
    재편입) 새 구간 — 재편입이 편출 기간을 소급해 덮지 않는다."""
    n = 0
    d = day.isoformat()
    for t in sorted({str(x) for x in tickers if x}):
        rows = conn.execute(
            "SELECT first_seen, last_seen FROM universe_members WHERE market=? AND ticker=?",
            (market, t),
        ).fetchall()
        hit = None
        for r in rows:
            lo, hi = date.fromisoformat(r[0]), date.fromisoformat(r[1])
            if lo - MERGE_GAP <= day <= hi + MERGE_GAP:
                hit = (r[0], lo, hi)
                break
        if hit is None:
            conn.execute(
                "INSERT INTO universe_members(market, ticker, first_seen, last_seen, source) "
                "VALUES (?,?,?,?,?)",
                (market, t, d, d, source),
            )
        else:
            key, lo, hi = hit
            conn.execute(
                "UPDATE universe_members SET first_seen=?, last_seen=? "
                "WHERE market=? AND ticker=? AND first_seen=?",
                (min(lo, day).isoformat(), max(hi, day).isoformat(), market, t, key),
            )
        n += 1
    conn.commit()
    return n


def members(conn: sqlite3.Connection, market: str) -> pd.DataFrame:
    """구간 목록 [ticker, first_seen, last_seen] (한 종목이 여러 행일 수 있다)."""
    rows = conn.execute(
        "SELECT ticker, first_seen, last_seen FROM universe_members WHERE market=? "
        "ORDER BY ticker, first_seen",
        (market,),
    ).fetchall()
    return pd.DataFrame(
        [(r[0], date.fromisoformat(r[1]), date.fromisoformat(r[2])) for r in rows],
        columns=["ticker", "first_seen", "last_seen"],
    )


def _liquidity_mask(
    df: pd.DataFrame, top_n: int, window: int, eligible: pd.Series | None = None
) -> pd.Series:
    """그날까지의 window 일 평균 거래대금(종가×거래량) 순위가 (날짜·시장) 안 top_n 이하인 행.
    (시장·종목·날짜) 순으로 굴려 입력 순서·다른 시장의 같은 코드와 섞이지 않는다. 평균은
    **걸러내기 전 전체 봉**으로 내고(새로 편입한 종목도 그 전 거래대금이 있다) 순위는 eligible
    행끼리만 매긴다."""
    eligible = pd.Series(True, index=df.index) if eligible is None else eligible
    order = df.sort_values(["market", "ticker", "date"])
    turnover = (order["close"].astype(float) * order["volume"].astype(float)).fillna(0.0)
    trailing = turnover.groupby([order["market"], order["ticker"]]).transform(
        lambda s: s.rolling(window, min_periods=1).mean()
    )
    trailing = trailing.reindex(df.index)[eligible]
    sub = df[eligible]
    rank = trailing.groupby([sub["date"], sub["market"]]).rank(ascending=False, method="first")
    return (rank <= top_n).reindex(df.index, fill_value=False)


def _member_mask(df_m: pd.DataFrame, mem: pd.DataFrame) -> pd.Series:
    """구간 중 하나라도 date 를 덮으면(마지막 기록 + GRACE 까지) 구성원."""
    days = df_m["date"].dt.date
    out = pd.Series(False, index=df_m.index)
    for t, spans in mem.groupby("ticker"):
        rows = df_m["ticker"] == t
        if not rows.any():
            continue
        d = days[rows]
        ok = pd.Series(False, index=d.index)
        for lo, hi in zip(spans["first_seen"], spans["last_seen"], strict=True):
            ok |= (d >= lo) & (d <= hi + GRACE)
        out[ok.index] = ok
    return out


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
        member = pd.Series(False, index=df.index)
        member[in_m] = _member_mask(df[in_m], mem)
        keep &= ~recorded | member
        if cfg.strict:
            keep &= ~in_m | recorded
        n_m = int(in_m.sum())
        info["markets"][market] = {
            "recorded_since": start.isoformat(),
            "coverage": round(int(recorded.sum()) / n_m, 4) if n_m else 0.0,
            "dropped_non_member": int((recorded & ~member).sum()),
        }
    if cfg.liquidity_top_n > 0 and keep.any():
        keep &= _liquidity_mask(df, cfg.liquidity_top_n, cfg.liquidity_window, keep)
    out = df[keep]
    info["rows_before"], info["rows_after"] = total, len(out)
    covered = [v["coverage"] for v in info["markets"].values()]
    info["coverage"] = round(min(covered), 4) if covered else 0.0
    return out.reset_index(drop=True), info
