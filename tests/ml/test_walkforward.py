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
        walkforward.check_no_leak(days, f.train_end, f.test_start, horizon_days=H, embargo_days=E)
    # 거래일 인덱스 기준: 간격이 (H+E) 거래일 이하이면 누출
    with pytest.raises(ValueError):
        walkforward.check_no_leak(days, days[199], days[209], horizon_days=H, embargo_days=E)
    walkforward.check_no_leak(days, days[199], days[210], horizon_days=H, embargo_days=E)
    with pytest.raises(ValueError):
        walkforward.check_no_leak(days, days[199], days[203], horizon_days=H, embargo_days=E)


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


def test_shuffled_labels_give_chance_accuracy():
    """라벨을 섞으면 정확도가 기준선 근처로 떨어진다(라벨과 무관하게 정확도를 올리는 요소 없음),
    신호 데이터에서는 학습된다. 피처 누출 자체는 NON_FEATURE_COLUMNS 거부·스토어 스키마가 막는다."""
    import numpy as np
    from helpers_ml import fast_cfg

    from trader.ml.models import quant

    df = synthetic_features(6, 320, signal=True)
    df = dataset.make_labels(df, horizon_days=H, up=0.02, down=-0.02)
    cols = dataset.feature_columns(df)
    real = quant.evaluate_walkforward(df, fast_cfg(), features=cols)
    shuffled = df.copy()
    rng = np.random.default_rng(1)
    mask = shuffled["label"].notna()
    shuffled.loc[mask, "label"] = rng.permutation(shuffled.loc[mask, "label"].to_numpy())
    fake = quant.evaluate_walkforward(shuffled, fast_cfg(), features=cols)
    assert real["accuracy"] > fake["accuracy"] + 0.05
    assert abs(fake["accuracy"] - fake["baseline"]) < 0.12
