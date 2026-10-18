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
