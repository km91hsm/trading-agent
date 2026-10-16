from datetime import timedelta

import pytest
from helpers_ml import synthetic_features, trading_days

from trader.ml import dataset, walkforward

H, E = 5, 5


def test_folds_non_overlapping_and_embargoed():
    days = trading_days(300)
    fs = walkforward.folds(days, n_folds=6, horizon_days=H, embargo_days=E)
    assert len(fs) == 6 and [f.index for f in fs] == list(range(6))
    idx = {d: i for i, d in enumerate(days)}
    for a, b in zip(fs, fs[1:], strict=False):
        assert a.test_end < b.test_start  # 테스트 창 서로소·오름차순
    for f in fs:
        assert f.train_end < f.test_start <= f.test_end
        assert idx[f.train_end] <= idx[f.test_start] - (H + E) - 1  # 엠바고 간격
    assert fs[-1].test_end == days[-1]


def test_train_label_windows_end_before_test_start():
    days = trading_days(300)
    fs = walkforward.folds(days, n_folds=4, horizon_days=H, embargo_days=E)
    idx = {d: i for i, d in enumerate(days)}
    for f in fs:
        train_days = [d for d in days if d <= f.train_end]
        # 학습 행 라벨은 date + H 거래일 뒤 종가를 쓴다 → 그 날짜가 전부 test_start 이전
        latest_label_day = days[idx[train_days[-1]] + H]
        assert latest_label_day < f.test_start
        walkforward.check_no_leak(train_days, f.test_start, horizon_days=H, embargo_days=E)
    with pytest.raises(ValueError):
        walkforward.check_no_leak(days[:200], days[203], horizon_days=H, embargo_days=E)


def test_small_history_drops_folds():
    days = trading_days(80)
    fs = walkforward.folds(days, n_folds=6, horizon_days=H, embargo_days=E, min_train=60)
    assert len(fs) <= 1
    assert walkforward.folds(days[:30], n_folds=6, horizon_days=H, embargo_days=E) == []


def test_folds_accept_unsorted_duplicate_dates():
    days = trading_days(200)
    mixed = days[::-1] + days[:50]
    fs = walkforward.folds(mixed, n_folds=3, horizon_days=H, embargo_days=E)
    assert fs and fs[0].test_start > fs[0].train_end + timedelta(days=H + E)


def test_dataset_rows_for_fold_respect_embargo():
    df = synthetic_features(2, 120, signal=False)
    df = dataset.make_labels(df, horizon_days=H, up=0.02, down=-0.02)
    days = sorted(df["date"].unique())
    fs = walkforward.folds(days, n_folds=2, horizon_days=H, embargo_days=E, min_train=30)
    train, test = walkforward.split(df, fs[0])
    assert train["date"].max() == fs[0].train_end and test["date"].min() == fs[0].test_start
    assert train["label"].notna().all() and len(test) > 0
