"""정량 필터·점수 (스펙 §5.1). 피처 스토어 최신 행만 쓰고 네트워크를 쓰지 않는다.

규칙: 거래대금(close×volume) 하위 30% 제외, 변동성(atr_14/close) 상위 10% 제외,
점수 = z(roc_10)·0.5 + z(high_52w_pos)·0.3 + z(log 거래대금)·0.2, 상한 30.
ML 랭킹(Phase 7)은 `rank_fn` 으로 끼운다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd

from trader.config import Universe
from trader.control.control import split_market
from trader.ml import features

logger = logging.getLogger("trader.scan")
TOP_N = 30
TURNOVER_DROP_Q = 0.30
VOLATILITY_DROP_Q = 0.90


def _z(s: pd.Series) -> pd.Series:
    return features.zscore(s, fill=0.0)


def score(df: pd.DataFrame, *, top: int = TOP_N) -> pd.DataFrame:
    """ticker·market·score·reasons 를 점수 내림차순으로 돌려준다 (빈 입력은 빈 프레임)."""
    cols = ["ticker", "market", "score", "reasons"]
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)
    d = df.copy()
    for c in ("atr_14", "roc_10", "high_52w_pos", "close", "volume"):
        if c not in d.columns:
            d[c] = np.nan
    d["turnover"] = pd.to_numeric(d["close"], errors="coerce") * pd.to_numeric(
        d["volume"], errors="coerce"
    )
    d["vol_ratio"] = pd.to_numeric(d["atr_14"], errors="coerce") / pd.to_numeric(
        d["close"], errors="coerce"
    )
    reasons: dict[int, list[str]] = {i: [] for i in d.index}
    keep = pd.Series(True, index=d.index)
    if d["turnover"].notna().sum() >= 3:
        cut = d["turnover"].quantile(TURNOVER_DROP_Q)
        low = d["turnover"] <= cut
        keep &= ~low
    if d["vol_ratio"].notna().sum() >= 3:
        cut = d["vol_ratio"].quantile(VOLATILITY_DROP_Q)
        high = d["vol_ratio"] > cut
        keep &= ~high
    d = d[keep].copy()
    # 신호 열이 NaN 인 행(지표 워밍업·결측)은 z=0 으로 통과시키지 않고 제외한다.
    # 열 전체가 비어 있으면(아직 계산하지 않는 지표) 그 열은 보지 않는다.
    signal_ok = pd.Series(True, index=d.index)
    for c in ("roc_10", "high_52w_pos", "turnover"):
        col = pd.to_numeric(d[c], errors="coerce")
        if col.notna().any():
            signal_ok &= col.notna()
    dropped = int((~signal_ok).sum())
    if dropped:
        logger.info("정량 필터: 신호 결측 %d행 제외", dropped)
    d = d[signal_ok].copy()
    if d.empty:
        return pd.DataFrame(columns=cols)
    z_roc = _z(d["roc_10"])
    z_pos = _z(d["high_52w_pos"])
    z_to = _z(np.log1p(d["turnover"].clip(lower=0)))
    d["score"] = (z_roc * 0.5 + z_pos * 0.3 + z_to * 0.2).round(4)
    for i in d.index:
        r = reasons[i]
        if z_roc[i] > 0.5:
            r.append("모멘텀 상위")
        if z_pos[i] > 0.5:
            r.append("52주 고점 근접")
        if z_to[i] > 0.5:
            r.append("거래대금 상위")
        if not r:
            r.append("필터 통과")
    d["reasons"] = [reasons[i] for i in d.index]
    out = d.sort_values("score", ascending=False).head(top)
    return out[cols].reset_index(drop=True)


def latest_features(
    market: str, *, state: Path | None = None, tickers: list[str] | None = None
) -> pd.DataFrame:
    """피처 스토어의 종목별 최신 행 (최신 파일부터 부분 읽기, tickers 가 전부 모이면 중단)."""
    return features.latest_rows(state, market, tickers=tickers)


def candidates(
    universe: Universe,
    market: str,
    *,
    state: Path | None = None,
    rank_fn: Callable[[pd.DataFrame], pd.DataFrame] | None = None,
    exclude: set[str] | None = None,
    watch_extra: list[str] | None = None,
) -> list[dict]:
    """유니버스(+watch+런타임 관심종목) 를 점수순으로. 피처 없는 종목은 점수 None 으로 끝에."""
    excl = set(universe.exclude) | (exclude or set())
    if market == "KR":
        ids = [(t, None) for t in universe.kr]
    else:
        ids = [(u.symbol, u.exchange) for u in universe.us]
    for w in [*universe.watch, *(watch_extra or [])]:
        # 관심종목은 `KR:`/`US:` 접두사 또는 코드 형식(6자리 숫자=KR)으로 시장을 가린다
        wm, wt = split_market(str(w).strip().upper())
        if wm != market:
            continue
        if all(wt != t for t, _ in ids):
            ids.append((wt, None))
    ids = [(t, e) for t, e in ids if t not in excl]
    exch = dict(ids)
    df = latest_features(market, state=state, tickers=list(exch))
    df = df[df["ticker"].isin(exch)] if not df.empty else df
    scored = (rank_fn or score)(df) if not df.empty else pd.DataFrame()
    out: list[dict] = []
    seen: set[str] = set()
    for _, r in scored.iterrows():
        out.append(
            {
                "ticker": str(r["ticker"]),
                "exchange": exch.get(str(r["ticker"])),
                "score": float(r["score"]),
                "reasons": list(r["reasons"]),
            }
        )
        seen.add(str(r["ticker"]))
    for t, e in ids:
        if t not in seen:
            out.append({"ticker": t, "exchange": e, "score": None, "reasons": ["피처 없음"]})
    return out
