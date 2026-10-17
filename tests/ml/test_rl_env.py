import sys

import pytest
from helpers_rl import steps_of as _steps

from trader.config import ConfigError
from trader.ml.rl.env import OBS_SIZE, SizingEnv, episode_metrics, gym_env


def test_env_obs_size_and_nav_compounding():
    env = SizingEnv(_steps([0.10, -0.10]), max_weight=0.25, drawdown_penalty=0.0, cost_bp=0)
    obs, _ = env.reset(seed=0)
    assert obs.shape == (OBS_SIZE,)
    _, r1, done, _, info = env.step(0.5)  # 0.25 로 클립
    assert not done and r1 == pytest.approx(0.025) and info["nav"] == pytest.approx(1.025)
    _, r2, done, _, info = env.step(0.25)
    assert done and info["nav"] == pytest.approx(1.025 * 0.975)
    m = episode_metrics(env)
    assert m["mdd"] == pytest.approx(-0.025) and m["steps"] == 2


def test_env_penalizes_drawdown_and_turnover():
    env = SizingEnv(_steps([-0.10, -0.10]), max_weight=0.25, drawdown_penalty=2.0, cost_bp=100)
    env.reset()
    _, r, *_ = env.step(0.25)
    # w·r = −0.025, 회전 비용 0.01·0.25 = 0.0025, 드로다운 증가 0.025 × 2
    assert r == pytest.approx(-0.025 - 0.0025 - 0.05)


def test_env_rejects_empty_steps():
    with pytest.raises(ValueError):
        SizingEnv(_steps([]), max_weight=0.25, drawdown_penalty=1.0, cost_bp=30)


def test_gym_env_requires_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "gymnasium", None)
    with pytest.raises(ConfigError):
        gym_env(SizingEnv(_steps([0.0]), max_weight=0.25, drawdown_penalty=1.0, cost_bp=30))


def test_env_splits_pnl_across_same_date_and_horizon():
    import pandas as pd

    two = _steps([0.10, 0.10])
    two["date"] = [pd.Timestamp("2025-01-02")] * 2  # 같은 날 2종목
    env = SizingEnv(two, max_weight=0.25, drawdown_penalty=0.0, cost_bp=0)
    env.reset()
    _, r1, *_ = env.step(0.25)
    _, r2, _, _, info = env.step(0.25)
    assert r1 == pytest.approx(0.0125) and info["nav"] == pytest.approx(1.0125**2)
    env5 = SizingEnv(
        _steps([0.10]), max_weight=0.25, drawdown_penalty=0.0, cost_bp=0, horizon_days=5
    )
    env5.reset()
    _, r, *_ = env5.step(0.25)
    assert r == pytest.approx(0.025 / 5)
