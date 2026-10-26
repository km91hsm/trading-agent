"""RL 스텝 데이터셋 (스펙 §9.4).

한 스텝 = (날짜, 종목, 관측 14차원, 전방 수익). 만기 메모리(실제 결정·결과)가 `min_memory_steps`
이상이면 그것만 쓰고, 아니면 피처 스토어의 모든 행을 "확신 0.5·LLM 비중 0 의 가상 BUY" 로 본다.
전방 수익은 같은 종목 `horizon_days` 뒤 종가 수익이라 미래 봉이 없는 마지막 행은 빠진다.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd

from trader.config import KST
from trader.memory import vector
from trader.ml import dataset

OBS_COLUMNS: tuple[str, ...] = (*vector.DIMS, "confidence", "llm_weight")
STEP_COLUMNS = ["date", "ticker", "obs", "fwd_return", "source"]
MEMORY_COLUMNS = [*STEP_COLUMNS, "horizon_days"]  # 결정별 horizon (엠바고·보상 정규화용)


def _empty() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in STEP_COLUMNS})


def _run_day(iso: str) -> pd.Timestamp:
    """decided_at(UTC) → 운영일(KST 날짜) 자정 타임스탬프.

    UTC 날짜로 묶으면 아침 결정(전날 22:50 UTC)이 전날 스텝과 섞여 폴드 경계가 하루 어긋난다."""
    ts = pd.Timestamp(iso)
    if ts.tzinfo is not None:
        ts = ts.tz_convert(KST)
    return pd.Timestamp(ts.date())


def _memory_steps(conn: sqlite3.Connection, market: str | None) -> pd.DataFrame:
    sql = (
        "SELECT decided_at, ticker, situation_vector, decision, price_return, horizon_days "
        "FROM memory_entries "
        "WHERE price_return IS NOT NULL AND round=0"
    )
    args: list = []
    if market:
        sql += " AND market=?"
        args.append(market)
    rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        dec = json.loads(r["decision"] or "{}")
        if str(dec.get("action") or "").upper() not in ("BUY", "ADD"):
            continue  # 사이저는 매수 비중만 다룬다: SELL/TRIM/HOLD 를 매수로 취급하지 않는다
        vec = [float(x) for x in json.loads(r["situation_vector"])]
        conf = float(dec.get("confidence") if dec.get("confidence") is not None else 0.5)
        w = float(dec.get("target_weight") or 0.0)
        out.append(
            {
                "date": _run_day(r["decided_at"]),
                "ticker": r["ticker"],
                "obs": [*vec, conf, w],
                "fwd_return": float(r["price_return"]),
                "source": "memory",
                "horizon_days": int(r["horizon_days"] or 1),
            }
        )
    return pd.DataFrame(out, columns=MEMORY_COLUMNS) if out else _empty()


def _feature_steps(state: Path | None, market: str | None, horizon_days: int) -> pd.DataFrame:
    df = dataset.load_all(state, market=market)
    if df.empty:
        return _empty()
    df = dataset.make_labels(df, horizon_days=horizon_days, up=0.0, down=0.0)
    df = df[df["fwd_return"].notna()]
    if df.empty:
        return _empty()
    out = []
    for row in df.to_dict("records"):
        mk = str(row.get("market") or "KR")
        out.append(
            {
                "date": pd.Timestamp(row["date"]),
                "ticker": row["ticker"],
                "obs": [*vector.situation_vector(row, mk), 0.5, 0.0],
                "fwd_return": float(row["fwd_return"]),
                "source": "features",
            }
        )
    return pd.DataFrame(out, columns=STEP_COLUMNS)


def build_steps(
    conn: sqlite3.Connection,
    state: Path | None,
    *,
    market: str | None,
    horizon_days: int,
    min_memory_steps: int,
) -> pd.DataFrame:
    """열 `date, ticker, obs(14), fwd_return, source[, horizon_days]`, (date, ticker) 오름차순."""
    mem = _memory_steps(conn, market)
    steps = mem if len(mem) >= min_memory_steps else _feature_steps(state, market, horizon_days)
    if steps.empty:
        return _empty()
    return steps.sort_values(["date", "ticker"]).reset_index(drop=True)
