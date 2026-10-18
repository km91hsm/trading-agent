"""만기 결정의 결과 채우기 (스펙 §6.5): 피처 스토어 종가로 실현수익·1/N 벤치마크 대비 알파.

결정일 D 의 종가 → D 에서 horizon 거래일 뒤 종가. BUY/ADD 는 그대로, SELL/TRIM 은 부호 반전,
HOLD 는 실현수익 0(보유 안 함) 이고 알파 = −벤치마크. 종가가 없거나 만기 전이면 건너뛴다.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd

from trader.config import KST
from trader.memory import store
from trader.ml import dataset

logger = logging.getLogger("trader.memory")
WINDOW_LEAD_DAYS = 10  # 결정일 직전 거래일 탐색 여유
SIGN = {"BUY": 1.0, "ADD": 1.0, "SELL": -1.0, "TRIM": -1.0, "HOLD": 0.0}


def _close_table(
    state: Path | None, market: str, *, since: date | None = None, until: date | None = None
) -> pd.DataFrame:
    # 만기 계산은 미결 결정일 이후 봉만 필요하다 — 스토어 전체(1년에 250파일씩 증가)를 읽지 않는다
    df = dataset.load_all(state, market=market, since=since, until=until)
    if df.empty:
        return pd.DataFrame()
    df = df[["date", "ticker", "close"]].copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df.pivot_table(
        index="date", columns="ticker", values="close", aggfunc="last"
    ).sort_index()


def window_return(
    table: pd.DataFrame, ticker: str, start: date, horizon: int, *, end_before: date | None = None
) -> tuple[float, float] | None:
    """(종목 수익, 1/N 벤치마크 수익): 시작일 이후 첫 거래일 → horizon 거래일 뒤. 없으면 None.
    end_before 가 있으면 만기 봉 날짜가 그 날 이후일 때 None(백필 데이터로 미래를 보지 않음)."""
    if table.empty or ticker not in table.columns:
        return None
    dates = list(table.index)
    starts = [i for i, d in enumerate(dates) if d >= start]
    if not starts:
        return None
    i0 = starts[0]
    i1 = i0 + horizon
    if i1 >= len(dates) or (end_before is not None and dates[i1] > end_before):
        return None
    c0, c1 = table.iloc[i0][ticker], table.iloc[i1][ticker]
    if pd.isna(c0) or pd.isna(c1) or float(c0) <= 0:
        return None
    r = float(c1) / float(c0) - 1
    row0, row1 = table.iloc[i0], table.iloc[i1]
    both = (~row0.isna()) & (~row1.isna()) & (row0 > 0)
    bench = float((row1[both] / row0[both] - 1).mean()) if both.any() else 0.0
    return r, bench


def mature(conn, *, state: Path | None, today: date, horizon_default: int = 5) -> list[dict]:
    """outcome 이 없는 엔트리 중 만기가 지난 것의 결과를 채우고 요약을 돌려준다 (멱등)."""
    rows = conn.execute(
        "SELECT id, ticker, market, decided_at, decision, horizon_days FROM memory_entries "
        "WHERE outcome_at IS NULL ORDER BY id"
    ).fetchall()
    earliest: dict[str, date] = {}
    for r in rows:
        try:
            d0 = datetime.fromisoformat(r["decided_at"]).astimezone(KST).date()
        except ValueError:
            continue
        m = r["market"]
        earliest[m] = min(earliest.get(m, d0), d0)
    tables: dict[str, pd.DataFrame] = {}
    out: list[dict] = []
    for r in rows:
        market = r["market"]
        if market not in tables:
            since = (
                earliest[market] - timedelta(days=WINDOW_LEAD_DAYS) if market in earliest else None
            )
            tables[market] = _close_table(state, market, since=since, until=today)
        try:
            decided = datetime.fromisoformat(r["decided_at"]).astimezone(KST).date()
        except ValueError:
            continue
        horizon = int(r["horizon_days"] or horizon_default)
        res = window_return(tables[market], r["ticker"], decided, horizon, end_before=today)
        if res is None:
            continue
        ret, bench = res
        action = str((json.loads(r["decision"] or "{}") or {}).get("action") or "HOLD").upper()
        sign = SIGN.get(action, 0.0)
        realized = ret * sign
        alpha = realized - bench
        outcome_at = datetime.now(UTC).isoformat()
        store.fill_outcome(
            conn,
            int(r["id"]),
            realized_return=realized,
            alpha=alpha,
            outcome_at=outcome_at,
            price_return=ret,
        )
        out.append(
            {
                "entry_id": int(r["id"]),
                "ticker": r["ticker"],
                "market": market,
                "realized_return": round(realized, 6),
                "alpha": round(alpha, 6),
            }
        )
    return out
