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
