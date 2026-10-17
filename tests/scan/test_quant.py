from datetime import date

import pandas as pd

from trader.config import Universe
from trader.scan import quant


def _df():
    rows = []
    for i in range(10):
        rows.append(
            {
                "date": date(2026, 10, 8),
                "ticker": f"T{i:02d}",
                "market": "KR",
                "close": 1000.0 + i,
                "volume": (i + 1) * 100_000,  # T00 거래대금 최저
                "atr_14": 10.0 if i != 9 else 400.0,  # T09 변동성 극단
                "roc_10": float(i),  # 모멘텀은 번호 순
                "high_52w_pos": 0.1 * i,
                "vol_z_20": 0.0,
            }
        )
    return pd.DataFrame(rows)


def test_quant_filters_and_scores():
    out = quant.score(_df(), top=30)
    tickers = list(out["ticker"])
    assert "T09" not in tickers  # 변동성 상위 10% 제외
    assert (
        "T00" not in tickers and "T01" not in tickers and "T02" not in tickers
    )  # 거래대금 하위 30%
    assert tickers[0] == "T08" and out["score"].is_monotonic_decreasing
    assert all(isinstance(r, list) for r in out["reasons"])
    assert len(quant.score(_df(), top=2)) == 2


def test_quant_handles_missing_indicator_columns():
    df = _df().drop(columns=["atr_14", "high_52w_pos"])
    out = quant.score(df, top=30)
    assert len(out) >= 1 and out["score"].notna().all()


def test_candidates_merges_universe_watch_and_exclude(project):
    from trader.ml import features

    df = _df()
    df["ticker"] = [f"0000{i:02d}" for i in range(10)]
    features.write_rows(_full(df))
    uni = Universe(
        kr=[f"0000{i:02d}" for i in range(10)] + ["999999"], watch=["000001"], exclude=["000008"]
    )
    cands = quant.candidates(uni, "KR")
    ids = [c["ticker"] for c in cands]
    assert "000008" not in ids and "000001" in ids  # exclude 제거, watch 포함(점수와 무관)
    assert ids[0] == "000007"  # 000008 제외 후 최고 점수
    assert "999999" in ids and cands[-1]["score"] is None  # 피처 없는 종목은 끝에
    assert all(c["exchange"] is None for c in cands)


def test_candidates_us_carries_exchange(project):
    uni = Universe(us=[{"symbol": "AAPL", "exchange": "NAS"}])
    cands = quant.candidates(uni, "US")
    assert cands == [{"ticker": "AAPL", "exchange": "NAS", "score": None, "reasons": ["피처 없음"]}]


def _full(df):
    """피처 스토어가 요구하는 전체 열을 채운다 (없는 열은 NaN)."""
    import numpy as np

    from trader.ml import features as _f

    for c in _f.FEATURE_COLUMNS:
        if c not in df.columns:
            df[c] = np.nan
    return df
