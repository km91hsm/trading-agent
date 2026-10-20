import numpy as np
import pytest
from helpers_ml import synthetic_features

from trader.config import MlConfig
from trader.ml import dataset
from trader.ml.models import quant


def _labeled(signal: bool, n_days=300, n_tickers=6):
    df = synthetic_features(n_tickers, n_days, signal=signal)
    return dataset.make_labels(df, horizon_days=5, up=0.02, down=-0.02)


def test_quant_model_fits_and_predicts_shape():
    df = _labeled(signal=True)
    cols = dataset.feature_columns(df)
    X, y = dataset.split_xy(df, cols)
    m = quant.QuantModel(cols).fit(X, y)
    p = m.predict_proba(X[:7])
    assert p.shape == (7, 3) and np.allclose(p.sum(axis=1), 1.0)
    assert m.backend in ("sklearn", "lightgbm") and m.features == cols
    imp = m.importances(X[:200], y[:200])
    assert set(imp) == set(cols) and all(isinstance(v, float) for v in imp.values())


def test_align_fills_missing_and_ignores_extra():
    df = _labeled(signal=False, n_days=60, n_tickers=2)
    cols = dataset.feature_columns(df)
    sub = df.drop(columns=[cols[0]]).assign(extra_col=1.0)
    X = quant.align(sub, cols)
    assert X.shape == (len(sub), len(cols)) and np.isnan(X[:, 0]).all()


def test_future_leak_column_is_rejected():
    df = _labeled(signal=False, n_days=60, n_tickers=2)
    cols = dataset.feature_columns(df)
    assert "fwd_return" not in cols and "label" not in cols
    with pytest.raises(ValueError):
        dataset.split_xy(df, [*cols, "fwd_return"])
    with pytest.raises(ValueError):
        quant.QuantModel([*cols, "label"])


def test_evaluate_walkforward_reports_folds():
    from helpers_ml import fast_cfg

    df = _labeled(signal=True)
    cfg = fast_cfg()
    r = quant.evaluate_walkforward(df, cfg, features=dataset.feature_columns(df))
    assert len(r["folds"]) == 6 and r["n_test"] > 0
    f = r["folds"][0]
    assert {"index", "train_rows", "test_rows", "accuracy", "baseline", "alpha"} <= set(f)
    assert 0 <= r["accuracy"] <= 1 and 0 <= r["baseline"] <= 1


def test_quant_model_max_iter_is_configurable():

    m = quant.QuantModel(["roc_10"], backend="sklearn", max_iter=7)
    assert m._est.max_iter == 7
    cfg = MlConfig.model_validate({"models": {"quant": {"max_iter": 30}}})
    assert cfg.models.quant.max_iter == 30 and MlConfig().models.quant.max_iter == 200


def test_evaluate_walkforward_reports_per_day_topn_alpha():
    """승격에 쓰는 알파는 랭커의 실제 용도와 같아야 한다: 날짜(·시장)마다 p_up 상위 N 종목의
    fwd_return 평균 − 그날 유니버스 평균을 날짜 평균한 값(alpha_topn).
    풀링 알파는 타이밍이 섞인다."""
    from helpers_ml import fast_cfg, synthetic_features

    from trader.ml import dataset

    cfg = fast_cfg(train={"walk_forward_folds": 2}, label={"mode": "relative_tercile"})
    df = synthetic_features(8, 160, signal=True)
    df = dataset.make_labels(df, horizon_days=5, up=0.02, down=-0.02, mode="relative_tercile")
    m = quant.evaluate_walkforward(df, cfg, features=dataset.feature_columns(df))
    assert "alpha_topn" in m and all("alpha_topn" in f for f in m["folds"])
    assert all(abs(f["alpha_topn"]) < 1 for f in m["folds"])


def test_quant_model_takes_learning_rate_and_leaves():
    """하이퍼파라미터 탐색(ml tune)이 바꾸는 두 값이 추정기에 그대로 들어간다."""
    df = _labeled(signal=False, n_days=60, n_tickers=2)
    cols = dataset.feature_columns(df)
    m = quant.QuantModel(cols, learning_rate=0.1, num_leaves=7, max_iter=20)
    assert m._est.learning_rate == 0.1
    leaves = getattr(m._est, "max_leaf_nodes", None) or getattr(m._est, "num_leaves", None)
    assert leaves == 7


def test_evaluate_walkforward_fold_indices_subset():
    """fold_indices 를 주면 그 폴드만 평가한다 (tune 의 dev/홀드아웃 분리)."""
    from helpers_ml import fast_cfg

    df = _labeled(signal=True, n_days=300, n_tickers=6)
    cols = dataset.feature_columns(df)
    cfg = fast_cfg(train={"walk_forward_folds": 4})
    full = quant.evaluate_walkforward(df, cfg, features=cols)
    n = len(full["folds"])
    assert n >= 2
    dev = quant.evaluate_walkforward(df, cfg, features=cols, fold_indices=list(range(n - 1)))
    hold = quant.evaluate_walkforward(df, cfg, features=cols, fold_indices=[n - 1])
    assert [f["index"] for f in dev["folds"]] == list(range(n - 1))
    assert [f["index"] for f in hold["folds"]] == [n - 1]
    assert hold["folds"][0]["accuracy"] == full["folds"][-1]["accuracy"]


def test_quant_model_sklearn_disables_early_stopping():
    """HistGradientBoosting 의 early_stopping='auto' 는 1만 행 넘으면 무작위 10% 검증으로 조기
    종료해 max_iter 탐색이 무의미하고 시간 순서도 섞인다 → 명시적으로 끈다 (리뷰 #4)."""
    df = _labeled(signal=False, n_days=60, n_tickers=2)
    cols = dataset.feature_columns(df)
    m = quant.QuantModel(cols, backend="sklearn", max_iter=20)
    assert m._est.early_stopping is False
