"""ml tune: 하이퍼파라미터는 dev 폴드(마지막 폴드 제외)로만 고르고 홀드아웃은 보고만 한다."""

import json

from helpers_ml import fast_cfg, synthetic_features

from trader.ml import features, tune
from trader.ml.models import quant

TINY = {"learning_rate": [0.05, 0.1], "num_leaves": [7], "max_iter": [20]}


def _seed(project, n_days=320):
    features.write_rows(synthetic_features(6, n_days, signal=True))


def test_tune_selects_on_dev_folds_only(project, monkeypatch):
    _seed(project)
    calls: list[tuple[dict, list[int] | None]] = []

    def fake_eval(df, cfg, *, features, fold_indices=None):
        q = cfg.models.quant
        calls.append(({"learning_rate": q.learning_rate, "num_leaves": q.num_leaves}, fold_indices))
        # learning_rate 0.1 이 dev 에서 더 좋아 보이게
        a = 0.01 if q.learning_rate == 0.1 else 0.001
        folds = [
            {"index": i, "alpha_topn": a, "accuracy": 0.4, "up_count": 9} for i in fold_indices
        ]
        return {
            "folds": folds,
            "accuracy": 0.4,
            "baseline": 0.33,
            "alpha": a,
            "alpha_topn": a,
            "n_test": 10,
        }

    monkeypatch.setattr(quant, "evaluate_walkforward", fake_eval)
    monkeypatch.setitem(tune.GRIDS, "tiny", TINY)
    r = tune.tune_quant(fast_cfg(train={"walk_forward_folds": 4}), market="KR", grid="tiny")
    assert r["status"] == "tuned"
    n = r["n_folds"]
    dev, hold = list(range(n - 1)), [n - 1]
    cand_calls = [c for c in calls if c[1] == dev]
    hold_calls = [c for c in calls if c[1] == hold]
    assert len(cand_calls) == 2 and len(hold_calls) == 2  # 후보 2개는 dev, 홀드아웃은 기본·최선 2회
    assert all(c[1] in (dev, hold) for c in calls)  # 전체 폴드로 평가하는 호출은 없다
    assert r["best"]["params"]["learning_rate"] == 0.1
    assert r["dev_folds"] == dev and r["holdout_fold"] == n - 1


def test_tune_reports_default_and_best_holdout_and_saves_json(project, monkeypatch):
    _seed(project)
    monkeypatch.setitem(tune.GRIDS, "tiny", TINY)
    r = tune.tune_quant(fast_cfg(train={"walk_forward_folds": 4}), market="KR", grid="tiny")
    assert r["status"] == "tuned" and len(r["candidates"]) == 2
    for c in r["candidates"]:
        assert set(c["params"]) == {"learning_rate", "num_leaves", "max_iter"}
        assert set(c["dev"]) >= {"alpha_topn", "accuracy", "positive_folds"}
    assert set(r["holdout"]) == {"default", "best"}
    assert "alpha_topn" in r["holdout"]["default"] and "alpha_topn" in r["holdout"]["best"]
    assert r["applied"] is False
    files = list((project / "state" / "models" / "quant_tune").glob("*.json"))
    assert len(files) == 1 and r["saved"] == str(files[0])
    saved = json.loads(files[0].read_text(encoding="utf-8"))
    assert saved["best"] == r["best"]


def test_tune_insufficient_data(project):
    features.write_rows(synthetic_features(1, 30, signal=False))
    r = tune.tune_quant(fast_cfg(), market="KR")
    assert r["status"] == "insufficient_data"


def _fake_eval_factory(calls, *, alpha_by_lr, holdout_by_lr=None, drop_dev=False):
    def fake_eval(df, cfg, *, features, fold_indices=None):
        q = cfg.models.quant
        calls.append(fold_indices)
        if drop_dev and len(fold_indices) > 1:
            folds = []
        else:
            table = alpha_by_lr if len(fold_indices) > 1 else (holdout_by_lr or {})
            a = table.get(q.learning_rate, 0.0)
            folds = [
                {"index": i, "alpha_topn": a, "accuracy": 0.4, "up_count": 9} for i in fold_indices
            ]
        mean = sum(f["alpha_topn"] for f in folds) / len(folds) if folds else 0.0
        return {
            "folds": folds,
            "accuracy": 0.4,
            "baseline": 0.33,
            "alpha": mean,
            "alpha_topn": mean,
            "n_test": 10,
        }

    return fake_eval


def test_tune_insufficient_when_no_dev_fold_was_evaluated(project, monkeypatch):
    """폴드가 '생성' 돼도 학습창이 비어 평가되지 않으면 전부 0 으로 동률 → 첫 후보를 best 로
    보고하면 안 된다 (리뷰 #3)."""
    _seed(project)
    calls: list = []
    monkeypatch.setattr(
        quant, "evaluate_walkforward", _fake_eval_factory(calls, alpha_by_lr={}, drop_dev=True)
    )
    monkeypatch.setitem(tune.GRIDS, "tiny", TINY)
    r = tune.tune_quant(fast_cfg(train={"walk_forward_folds": 4}), market="KR", grid="tiny")
    assert r["status"] == "insufficient_folds"


def test_tune_tie_prefers_default_params_and_recommendation_needs_holdout(project, monkeypatch):
    _seed(project)
    cfg = fast_cfg(train={"walk_forward_folds": 4}, quant={"promote_min_alpha": 0.003})
    calls: list = []
    # 동률(모두 0.002) → 기본값(learning_rate 0.05) 우선
    monkeypatch.setattr(
        quant,
        "evaluate_walkforward",
        _fake_eval_factory(calls, alpha_by_lr={0.05: 0.002, 0.1: 0.002}),
    )
    monkeypatch.setitem(tune.GRIDS, "tiny", TINY)
    r = tune.tune_quant(cfg, market="KR", grid="tiny")
    assert r["best"]["params"]["learning_rate"] == 0.05
    assert r["recommendation"]["apply"] is False  # 현재 설정과 같음 → 바꿀 것 없음
    # dev 는 0.1 이 좋지만 홀드아웃 0.001 < promote_min_alpha 0.003 → 적용 권고 안 함
    monkeypatch.setattr(
        quant,
        "evaluate_walkforward",
        _fake_eval_factory(
            calls, alpha_by_lr={0.05: 0.001, 0.1: 0.004}, holdout_by_lr={0.05: 0.0, 0.1: 0.001}
        ),
    )
    r = tune.tune_quant(cfg, market="KR", grid="tiny")
    assert r["best"]["params"]["learning_rate"] == 0.1
    assert r["recommendation"]["apply"] is False and "홀드아웃" in r["recommendation"]["reason"]
    # 홀드아웃도 기준을 넘고 기본값보다 좋을 때만 권고
    monkeypatch.setattr(
        quant,
        "evaluate_walkforward",
        _fake_eval_factory(
            calls, alpha_by_lr={0.05: 0.001, 0.1: 0.004}, holdout_by_lr={0.05: 0.001, 0.1: 0.005}
        ),
    )
    r = tune.tune_quant(cfg, market="KR", grid="tiny")
    assert r["recommendation"]["apply"] is True
