from datetime import UTC, datetime

import numpy as np
from helpers_ml import synthetic_features

from trader.config import MlConfig
from trader.ml import dataset, features, registry, walkforward
from trader.ml.rl.env import OBS_SIZE
from trader.ml.rl.policy import FixedPolicy
from trader.ml.rl.train import train_sizer

NOW = datetime(2026, 10, 17, tzinfo=UTC)


def _fake_fit(steps, rl_cfg, *, seed):
    return FixedPolicy(0.2 if seed == 0 else 0.05), "fake"


def test_train_runs_folds_times_seeds_and_never_promotes(project, conn):
    features.write_rows(synthetic_features(4, 400, signal=True))
    cfg = MlConfig.model_validate(
        {"models": {"rl_sizer": {"seeds": 2}}, "train": {"walk_forward_folds": 3}}
    )
    r = train_sizer(cfg, conn, now=lambda: NOW, fit_fn=_fake_fit)
    assert r["status"] == "trained" and r["source"] == "features" and r["backend"] == "fake"
    assert len(r["folds"]) == 3 and [s["seed"] for s in r["folds"][0]["seeds"]] == [0, 1]
    assert set(r["summary"]) == {"rl", "fixed", "llm", "vol_target"}
    assert r["promoted"] is False and registry.latest("rl_sizer") is None
    meta = registry.list_models("rl_sizer")[0]
    assert meta["version"] == r["version"] and meta["promote_reason"].startswith("수동 승격")


def test_train_folds_respect_embargo(project, conn):
    features.write_rows(synthetic_features(2, 400, signal=False))
    r = train_sizer(MlConfig(), conn, now=lambda: NOW, fit_fn=_fake_fit)
    days = sorted(dataset.load_all()["date"].dt.date.unique())
    assert r["folds"]
    for f in r["folds"]:
        walkforward.check_no_leak(
            days, f["train_end"], f["test_start"], horizon_days=5, embargo_days=5
        )


def test_train_insufficient_data_saves_nothing(project, conn):
    features.write_rows(synthetic_features(1, 40, signal=False))
    r = train_sizer(MlConfig(), conn, now=lambda: NOW, fit_fn=_fake_fit)
    assert r["status"] == "insufficient_data" and r["version"] is None
    assert registry.list_models("rl_sizer") == []


def test_train_default_fit_is_cem_and_loadable(project, conn):
    features.write_rows(synthetic_features(3, 400, signal=True))
    cfg = MlConfig.model_validate(
        {
            "models": {
                "rl_sizer": {
                    "backend": "cem",
                    "cem_iterations": 2,
                    "cem_population": 8,
                    "seeds": 1,
                }
            }
        }
    )
    r = train_sizer(cfg, conn, now=lambda: NOW)
    assert r["backend"] == "cem"
    pol, meta = registry.load("rl_sizer", r["version"])
    assert 0 <= pol.act(np.zeros(OBS_SIZE)) <= 0.25 and meta["obs_columns"][-1] == "llm_weight"
