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


def _alpha_folds(alphas, up=20):
    return [
        {
            "index": i,
            "accuracy": 0.34,
            "baseline": 0.34,
            "alpha": a,
            "up_count": up,
            "test_rows": 100,
        }
        for i, a in enumerate(alphas)
    ]


def test_promotion_alpha_mode_requires_mean_and_fold_fraction():
    """순위 용도의 승격: 평균 알파 ≥ promote_min_alpha 이고 알파가 양인 폴드 비율 ≥ 기준.
    정확도는 보지 않는다 (상대 라벨에선 기준선이 1/3 로 고정돼 정확도 비교가 의미를 잃는다)."""
    cfg = MlConfig.model_validate(
        {
            "models": {
                "quant": {
                    "promote_metric": "alpha",
                    "promote_min_alpha": 0.003,
                    "promote_min_positive_folds": 0.67,
                }
            }
        }
    )
    good = _alpha_folds([0.005, 0.004, 0.006, -0.001, 0.003, 0.007])
    m = {"accuracy": 0.34, "baseline": 0.34, "alpha": 0.004, "folds": good, "n_test": 600}
    ok, why = train.promotion_decision(m, cfg)
    assert ok is True and "알파" in why
    few = _alpha_folds([0.01, 0.01, 0.01, -0.01, -0.01, -0.01])
    ok, why = train.promotion_decision({**m, "folds": few}, cfg)
    assert ok is False and "폴드" in why
    ok, why = train.promotion_decision({**m, "alpha": 0.002}, cfg)
    assert ok is False and "알파" in why


def test_promotion_alpha_mode_still_requires_up_predictions():
    cfg = MlConfig.model_validate(
        {"models": {"quant": {"promote_metric": "alpha", "promote_min_alpha": 0.001}}}
    )
    folds = _alpha_folds([0.01] * 6, up=2)
    ok, why = train.promotion_decision(
        {"accuracy": 0.34, "baseline": 0.34, "alpha": 0.01, "folds": folds, "n_test": 600}, cfg
    )
    assert ok is False and "up 예측" in why


def test_promotion_accuracy_mode_unchanged_by_new_fields():
    cfg = MlConfig()
    assert cfg.models.quant.promote_metric == "accuracy"
    assert train.promotion_decision(_metrics(0.52, 0.45, 0.002), cfg)[0] is True
    assert train.promotion_decision(_metrics(0.43, 0.50, 0.002), cfg)[0] is False


def test_promotion_alpha_mode_prefers_topn_alpha():
    cfg = MlConfig.model_validate(
        {"models": {"quant": {"promote_metric": "alpha", "promote_min_alpha": 0.003}}}
    )
    folds = [
        {
            "index": i,
            "accuracy": 0.34,
            "baseline": 0.34,
            "alpha": 0.01,
            "alpha_topn": -0.01,
            "up_count": 20,
            "test_rows": 100,
        }
        for i in range(6)
    ]
    m = {
        "accuracy": 0.34,
        "baseline": 0.34,
        "alpha": 0.01,
        "alpha_topn": -0.01,
        "folds": folds,
        "n_test": 600,
    }
    ok, why = train.promotion_decision(m, cfg)
    assert ok is False and "상위" in why  # 풀링 알파가 양이어도 상위 N 알파가 음이면 미승격


def test_promotion_alpha_mode_uses_market_specific_threshold():
    """승격 알파 기준은 왕복비용 기준이라 시장별로 다르다: KR 30bp(0.003), US 50bp(0.005).
    같은 0.0033 이 KR 은 승격, US 는 미승격이어야 한다."""
    cfg = MlConfig.model_validate(
        {
            "models": {
                "quant": {
                    "promote_metric": "alpha",
                    "promote_min_alpha": 0.003,
                    "promote_min_alpha_by_market": {"US": 0.005},
                    "promote_min_positive_folds": 0.66,
                }
            }
        }
    )
    folds = _alpha_folds([0.0033] * 6)
    m = {
        "accuracy": 0.36,
        "baseline": 0.33,
        "alpha": 0.001,
        "alpha_topn": 0.0033,
        "folds": folds,
        "n_test": 600,
    }
    assert train.promotion_decision(m, cfg, market="KR")[0] is True
    ok, why = train.promotion_decision(m, cfg, market="US")
    assert ok is False and "0.005" in why
    assert train.promotion_decision(m, cfg)[0] is True  # 시장 미지정은 기본값
    assert cfg.models.quant.min_alpha_for("US") == 0.005
    assert cfg.models.quant.min_alpha_for("KR") == 0.003


def test_train_reports_survivorship_caveat(project):
    """Stock D-037: 현재 구성 종목으로 과거를 학습하면 알파가 부풀려진다 — 결과·메타에 경고."""
    _write(signal=True)
    r = train.train_quant(fast_cfg(), now=lambda: NOW)
    assert r["caveats"] and "생존 편향" in r["caveats"][0]
    assert "생존 편향" in registry.latest("quant")[1]["caveats"][0]


def test_relative_labels_recomputed_within_population():
    """Codex: 상대 삼분위는 걸러진 모집단 안에서 — 빠진 종목의 큰 수익이 순위를 밀어내지 않는다."""
    import pandas as pd

    from trader.ml import dataset

    day = pd.Timestamp("2026-09-01")
    rets = {"A": 0.01, "B": 0.02, "C": 0.03, "X": 1.0, "Y": 2.0, "Z": 3.0}
    df = pd.DataFrame(
        [{"date": day, "market": "KR", "ticker": t, "fwd_return": r} for t, r in rets.items()]
    )
    members = df[df.ticker.isin(["A", "B", "C"])]
    if len(members) < dataset.MIN_CROSS_SECTION:
        members = pd.concat([members] * dataset.MIN_CROSS_SECTION, ignore_index=True)
    labels = dataset.relative_labels(members)
    by = dict(zip(members.ticker, labels, strict=True))
    assert by["A"] == 0.0 and by["C"] == 2.0


def test_strict_pit_aborts_when_ledger_unreadable(project, monkeypatch):
    _write(signal=True, n_tickers=3, n_days=60)
    cfg = fast_cfg(train={"pit": {"strict": True}})
    monkeypatch.setattr(train, "_apply_pit", lambda df, c, s: (df, {"error": "no such table"}))
    df, cols, reason = train.prepare_dataset(
        cfg, state=None, market=None, now=lambda: NOW, min_rows=10
    )
    assert df.empty and "strict" in reason
