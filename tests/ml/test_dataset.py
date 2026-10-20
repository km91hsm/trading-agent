import pathlib
from datetime import date

import pandas as pd
from helpers_ml import synthetic_features

from trader.ml import dataset, features


def _one(closes, ticker="A"):
    days = pd.bdate_range("2025-01-02", periods=len(closes))
    return pd.DataFrame(
        {
            "date": days.date,
            "ticker": ticker,
            "market": "KR",
            "close": closes,
            "volume": 1,
            "roc_10": 0.0,
        }
    )


def test_make_labels_thresholds_and_horizon():
    df = _one([100, 101, 103, 100, 97, 100, 100, 100])
    out = dataset.make_labels(df, horizon_days=2, up=0.02, down=-0.02)
    assert list(out["fwd_return"].round(4)[:6]) == [0.03, -0.0099, -0.0583, 0.0, 0.0309, 0.0]
    assert list(out["label"][:6]) == [2, 1, 0, 1, 2, 1]
    assert out["label"].isna().sum() == 2 and out["fwd_return"].isna().sum() == 2  # 마지막 h 행
    two = pd.concat([_one([100, 110, 120], "A"), _one([50, 50, 50], "B")], ignore_index=True)
    out = dataset.make_labels(two, horizon_days=1, up=0.02, down=-0.02)
    assert out[out["ticker"] == "A"]["label"].isna().sum() == 1  # 종목 경계를 넘어 shift 하지 않음
    assert out[out["ticker"] == "B"]["label"].dropna().tolist() == [1, 1]


def test_feature_columns_excludes_targets_and_raw():
    df = synthetic_features(2, 30, signal=False)
    df = dataset.make_labels(df, horizon_days=5, up=0.02, down=-0.02)
    cols = dataset.feature_columns(df)
    assert "fwd_return" not in cols and "label" not in cols and "date" not in cols
    assert "close" not in cols and "volume" not in cols and "ticker" not in cols
    assert {"roc_10", "rsi_14", "high_52w_pos"} <= set(cols)
    X, y = dataset.split_xy(df, cols)
    assert X.shape[1] == len(cols) and X.shape[0] == len(y) and y.dtype.kind == "i"
    assert len(y) == len(df) - 2 * 5  # 종목당 마지막 5행 제외


def test_load_all_concatenates_and_sorts(project):
    df = synthetic_features(2, 3, signal=False)
    features.write_rows(df)
    out = dataset.load_all()
    assert len(out) == 6 and list(out["ticker"][:3]) == ["T00"] * 3
    assert str(out["date"].dtype).startswith("datetime64")
    assert dataset.load_all(market="US").empty
    assert dataset.load_all(state=project / "nowhere").empty


def test_load_all_date_window_reads_only_matching_files(project, monkeypatch):
    features.write_rows(synthetic_features(1, 6, signal=False))
    files = sorted(features._features_dir(None).glob("*.parquet"))
    assert len(files) == 6
    read: list[str] = []
    real = pd.read_parquet

    def spy(path, *a, **k):
        read.append(pathlib.Path(path).stem)
        return real(path, *a, **k)

    monkeypatch.setattr(pd, "read_parquet", spy)
    since, until = files[1].stem, files[3].stem
    out = dataset.load_all(since=date.fromisoformat(since), until=date.fromisoformat(until))
    assert sorted(read) == [f.stem for f in files[1:4]]
    assert out["date"].min().date().isoformat() == since
    assert out["date"].max().date().isoformat() == until


def _panel(n_days=4, tickers=("A", "B", "C", "D", "E", "F"), market="KR", seed=0):
    """날짜마다 모든 종목이 있는 패널. 종목별 종가 경로가 달라 fwd_return 순위가 갈린다."""
    import numpy as np

    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2025-01-02", periods=n_days + 3)
    rows = []
    for t in tickers:
        px = 100 * np.cumprod(1 + rng.normal(0, 0.02, len(days)))
        for d, c in zip(days, px, strict=True):
            rows.append(
                {"date": d.date(), "ticker": t, "market": market, "close": float(c), "volume": 1}
            )
    return pd.DataFrame(rows)


def test_relative_tercile_labels_balanced_per_day():
    """횡단면 삼분위: 같은 날 6종목이면 라벨 0·1·2 가 각 2개 (기준선이 1/3 로 고정된다)."""
    df = dataset.make_labels(_panel(), horizon_days=1, up=0.02, down=-0.02, mode="relative_tercile")
    labeled = df[df["label"].notna()]
    for _, g in labeled.groupby("date"):
        assert sorted(g["label"].astype(int).tolist()) == [0, 0, 1, 1, 2, 2], g["date"].iloc[0]
    # fwd_return 가 가장 큰 종목이 2, 가장 작은 종목이 0
    g = labeled[labeled["date"] == labeled["date"].min()].sort_values("fwd_return")
    assert g["label"].astype(int).tolist() == [0, 0, 1, 1, 2, 2]


def test_relative_tercile_groups_by_market():
    """KR·US 가 같은 날 섞여 있어도 시장별로 따로 삼분위를 매긴다 (US 안에도 0 이 있다)."""
    kr = _panel(tickers=("A", "B", "C"), market="KR", seed=1)
    us = _panel(tickers=("X", "Y", "Z"), market="US", seed=2)
    us["close"] = us["close"] * 10  # 수준이 달라도 순위는 시장 안에서만
    both = pd.concat([kr, us])
    df = dataset.make_labels(both, horizon_days=1, up=0, down=0, mode="relative_tercile")
    labeled = df[df["label"].notna()]
    for (_, m), g in labeled.groupby(["date", "market"]):
        assert sorted(g["label"].astype(int).tolist()) == [0, 1, 2], m


def test_relative_tercile_nan_when_fewer_than_three():
    two = _panel(tickers=("A", "B"))
    df = dataset.make_labels(two, horizon_days=1, up=0, down=0, mode="relative_tercile")
    assert df["label"].isna().all()


def test_absolute_mode_is_default_and_unchanged():
    df = _one([100, 101, 103, 100, 97, 100, 100, 100])
    a = dataset.make_labels(df, horizon_days=2, up=0.02, down=-0.02)
    b = dataset.make_labels(df, horizon_days=2, up=0.02, down=-0.02, mode="absolute")
    assert a["label"].equals(b["label"])


def test_relative_tercile_ties_share_a_label():
    """같은 fwd_return(거래정지·보합)은 같은 라벨이어야 한다.

    행 순서로 동률을 쪼개면 정렬 순서가 라벨이 된다.
    """
    rows = []
    for t, c1 in zip("ABCDEF", [100, 100, 100, 102, 102, 105], strict=True):
        base = {"ticker": t, "market": "KR", "volume": 1}
        rows.append({"date": date(2025, 1, 2), "close": 100.0, **base})
        rows.append({"date": date(2025, 1, 3), "close": float(c1), **base})
    df = dataset.make_labels(
        pd.DataFrame(rows), horizon_days=1, up=0, down=0, mode="relative_tercile"
    )
    first = df[df["date"] == date(2025, 1, 2)].set_index("ticker")["label"]
    assert first["A"] == first["B"] == first["C"] and first["D"] == first["E"]
