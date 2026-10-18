from datetime import UTC, datetime

from helpers_ml import fast_cfg, synthetic_features

from trader.config import MlConfig
from trader.ml import features, registry, train

NOW = datetime(2026, 10, 17, tzinfo=UTC)


def _write(signal: bool, n_days=320, n_tickers=6):
    features.write_rows(synthetic_features(n_tickers, n_days, signal=signal))


def test_train_promotes_on_signal_and_not_on_noise(project):
    _write(signal=True)
    r = train.train_quant(fast_cfg(), now=lambda: NOW)
    assert r["status"] == "trained" and r["promoted"] is True and r["rows"] > 1000
    assert r["metrics"]["accuracy"] >= 0.40 and len(r["features"]) >= 5
    assert registry.latest("quant")[1]["version"] == r["version"]
    assert "importances" in registry.latest("quant")[1]


def test_train_not_promoted_on_noise(project):
    _write(signal=False, n_tickers=3, n_days=200)
    cfg = fast_cfg(quant={"promote_min_accuracy": 0.9})
    r = train.train_quant(cfg, now=lambda: NOW)
    assert r["status"] == "trained" and r["promoted"] is False and "정확도" in r["promote_reason"]
    assert registry.latest("quant") is None


def test_not_promoted_when_below_threshold(project):
    _write(signal=True)
    cfg = fast_cfg(quant={"promote_min_accuracy": 0.99})
    assert train.train_quant(cfg, now=lambda: NOW)["promoted"] is False


def test_train_reports_insufficient_data(project):
    _write(signal=False, n_tickers=1, n_days=30)
    r = train.train_quant(fast_cfg(), now=lambda: NOW)
    assert r["status"] == "insufficient_data" and r["version"] is None
    assert (
        train.train_quant(fast_cfg(), now=lambda: NOW, market="US")["status"] == "insufficient_data"
    )


# --- 리뷰 반영: 승격 게이트 ------------------------------------------------------------


def _metrics(acc, baseline, alpha, folds=None):
    folds = folds or [
        {
            "index": 0,
            "accuracy": acc,
            "baseline": baseline,
            "alpha": alpha,
            "up_count": 20,
            "test_rows": 100,
        }
    ]
    return {"accuracy": acc, "baseline": baseline, "alpha": alpha, "folds": folds, "n_test": 100}


def test_promotion_requires_beating_baseline_and_positive_alpha():
    cfg = MlConfig()
    ok, why = train.promotion_decision(_metrics(0.52, 0.45, 0.002), cfg)
    assert ok and "기준선" in why
    assert train.promotion_decision(_metrics(0.43, 0.50, 0.002), cfg)[0] is False  # 기준선 미달
    assert train.promotion_decision(_metrics(0.52, 0.45, 0.0), cfg)[0] is False  # 알파 0 은 불합격
    no_up = _metrics(
        0.52,
        0.45,
        0.002,
        folds=[
            {
                "index": 0,
                "accuracy": 0.52,
                "baseline": 0.45,
                "alpha": 0.0,
                "up_count": 0,
                "test_rows": 100,
            }
        ],
    )
    assert train.promotion_decision(no_up, cfg)[0] is False  # up 예측 없는 폴드
    few_up = _metrics(
        0.52,
        0.45,
        0.002,
        folds=[
            {
                "index": 0,
                "accuracy": 0.52,
                "baseline": 0.45,
                "alpha": 0.01,
                "up_count": 2,
                "test_rows": 100,
            }
        ],
    )
    assert train.promotion_decision(few_up, cfg)[0] is False  # 최소 예측 건수 미달
    assert train.promotion_decision(_metrics(0.0, 0.0, 0.0, folds=[]), cfg)[0] is False


def test_noise_is_not_promoted_under_default_config_across_seeds(project):
    for seed in range(4):
        features.write_rows(synthetic_features(5, 260, signal=False, seed=seed))
        r = train.train_quant(fast_cfg(), now=lambda: NOW)
        assert r["status"] == "trained" and r["promoted"] is False, (seed, r["promote_reason"])


def test_train_window_years_limits_rows(project):
    features.write_rows(synthetic_features(2, 600, signal=False))
    cfg = fast_cfg(train={"window_years": 1})
    r = train.train_quant(cfg, now=lambda: datetime(2027, 5, 1, tzinfo=UTC))
    assert r["rows"] < 2 * 300  # 최근 1년치만 (600 거래일 ≈ 2.4년)


def test_importances_come_from_holdout_fold(project):
    features.write_rows(synthetic_features(5, 320, signal=True))
    r = train.train_quant(fast_cfg(), now=lambda: NOW)
    meta = registry.list_models("quant")[0]
    assert meta["importances_scope"] == "last_fold_holdout" and meta["importances"]
    assert r["metrics"]["folds"][0]["up_count"] >= 0


def test_backend_config_pins_sklearn(project):
    from trader.ml.models import quant

    cfg = MlConfig.model_validate({"models": {"quant": {"backend": "sklearn"}}})
    assert cfg.models.quant.backend == "sklearn"
    m = quant.QuantModel(["roc_10"], backend="sklearn")
    assert m.backend == "sklearn"
    import pytest

    with pytest.raises(ValueError):
        MlConfig.model_validate({"models": {"quant": {"backend": "xgboost"}}})
