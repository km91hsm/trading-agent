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
        return {"folds": folds, "accuracy": 0.4, "baseline": 0.33, "alpha": a, "alpha_topn": a,
                "n_test": 10}

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
