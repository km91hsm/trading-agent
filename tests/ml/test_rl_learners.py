import sys

import joblib
import numpy as np
import pandas as pd
import pytest
from helpers_ml import trading_days

from trader.config import ConfigError, RlSizerCfg
from trader.ml.rl.env import OBS_SIZE, SizingEnv
from trader.ml.rl.learners import fit, fit_cem, fit_ppo, resolve_backend
from trader.ml.rl.policy import FixedPolicy, LinearPolicy, evaluate


def _trend_steps(n=120):
    """양의 수익이 atr_pct 낮을 때만 나온다: 사이저가 저변동에서 크게 가야 이득."""
    rng = np.random.default_rng(1)
    atr = rng.choice([0.01, 0.05], size=n)
    ret = np.where(atr < 0.03, 0.02, -0.02)
    obs = [[0.0] * 4 + [float(a)] + [0.0] * 9 for a in atr]
    return pd.DataFrame(
        {
            "date": pd.to_datetime(trading_days(n)),
            "ticker": "A",
            "obs": obs,
            "fwd_return": ret,
            "source": "features",
        }
    )


def test_cem_is_seed_deterministic_and_beats_fixed():
    cfg = RlSizerCfg(cem_iterations=10, cem_population=32)
    steps = _trend_steps()
    a, b = fit_cem(steps, cfg, seed=0), fit_cem(steps, cfg, seed=0)
    assert np.allclose(a.theta, b.theta)
    env = SizingEnv(steps, max_weight=0.25, drawdown_penalty=0.0, cost_bp=0)
    assert evaluate(env, a)["return"] > evaluate(env, FixedPolicy(0.10))["return"]


def test_resolve_backend_auto_falls_back_to_cem(monkeypatch):
    monkeypatch.setitem(sys.modules, "stable_baselines3", None)
    assert resolve_backend(RlSizerCfg(backend="auto")) == "cem"
    with pytest.raises(ConfigError):
        resolve_backend(RlSizerCfg(backend="ppo"))


def test_fit_dispatches_and_reports_backend():
    cfg = RlSizerCfg(backend="cem", cem_iterations=2, cem_population=8)
    pol, backend = fit(_trend_steps(40), cfg, seed=1)
    assert backend == "cem" and isinstance(pol, LinearPolicy)


def test_ppo_policy_roundtrips_joblib(tmp_path):
    pytest.importorskip("stable_baselines3")
    pol = fit_ppo(_trend_steps(40), RlSizerCfg(total_timesteps=256), seed=0)
    joblib.dump(pol, tmp_path / "p.joblib")
    back = joblib.load(tmp_path / "p.joblib")
    obs = np.zeros(OBS_SIZE)
    assert back.act(obs) == pytest.approx(pol.act(obs)) and 0 <= back.act(obs) <= 0.25
