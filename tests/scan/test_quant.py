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


def test_scan_includes_runtime_watchlist(project):
    """control 관심종목(/watch)은 universe.watch 와 같이 후보에 들어간다 (점수와 무관)."""
    from trader.ml import features

    df = _df()
    df["ticker"] = [f"0000{i:02d}" for i in range(10)]
    features.write_rows(_full(df))
    uni = Universe(kr=[f"0000{i:02d}" for i in range(5)], exclude=["000003"])
    cands = quant.candidates(uni, "KR", watch_extra=["000007", "000003", "777777"])
    ids = [c["ticker"] for c in cands]
    assert "000007" in ids and "777777" in ids and "000003" not in ids
    assert [c for c in cands if c["ticker"] == "000007"][0]["score"] is not None


def test_score_drops_rows_with_nan_signal():
    df = _df()
    df.loc[df["ticker"] == "T05", "roc_10"] = float("nan")
    out = quant.score(df)
    assert "T05" not in set(out["ticker"]) and out["score"].notna().all()


def test_candidates_scope_watch_to_market(project):
    """universe.watch 는 접두사·코드 형식으로 시장을 가려 다른 시장 후보에 섞이지 않는다."""
    uni = Universe(kr=["005930"], us=[], watch=["000660", "AAPL", "US:MSFT", "KR:000270"])
    kr = [c["ticker"] for c in quant.candidates(uni, "KR")]
    us = [c["ticker"] for c in quant.candidates(uni, "US")]
    assert kr == ["005930", "000660", "000270"]
    assert us == ["AAPL", "MSFT"]
