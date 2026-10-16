from datetime import UTC, datetime

from helpers_ml import synthetic_features

from trader.config import MlConfig
from trader.ml import features, registry, train

NOW = datetime(2026, 10, 17, tzinfo=UTC)


def _write(signal: bool, n_days=320, n_tickers=6):
    features.write_rows(synthetic_features(n_tickers, n_days, signal=signal))


def test_train_promotes_on_signal_and_not_on_noise(project):
    _write(signal=True)
    r = train.train_quant(MlConfig(), now=lambda: NOW)
    assert r["status"] == "trained" and r["promoted"] is True and r["rows"] > 1000
    assert r["metrics"]["accuracy"] >= 0.40 and len(r["features"]) >= 5
    assert registry.latest("quant")[1]["version"] == r["version"]
    assert "importances" in registry.latest("quant")[1]


def test_train_not_promoted_on_noise(project):
    _write(signal=False, n_tickers=3, n_days=200)
    cfg = MlConfig.model_validate({"models": {"quant": {"promote_min_accuracy": 0.9}}})
    r = train.train_quant(cfg, now=lambda: NOW)
    assert r["status"] == "trained" and r["promoted"] is False and "정확도" in r["promote_reason"]
    assert registry.latest("quant") is None


def test_not_promoted_when_below_threshold(project):
    _write(signal=True)
    cfg = MlConfig.model_validate({"models": {"quant": {"promote_min_accuracy": 0.99}}})
    assert train.train_quant(cfg, now=lambda: NOW)["promoted"] is False


def test_train_reports_insufficient_data(project):
    _write(signal=False, n_tickers=1, n_days=30)
    r = train.train_quant(MlConfig(), now=lambda: NOW)
    assert r["status"] == "insufficient_data" and r["version"] is None
    assert (
        train.train_quant(MlConfig(), now=lambda: NOW, market="US")["status"] == "insufficient_data"
    )
