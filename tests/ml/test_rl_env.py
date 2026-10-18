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


def test_env_turnover_and_position_are_per_ticker_and_scaled():
    import pandas as pd

    df = _steps([0.0, 0.0, 0.0])
    df["ticker"] = ["A", "B", "A"]
    df["date"] = [pd.Timestamp("2025-01-02")] * 3  # 같은 날 A·B·A(두 번째 A 는 다음 결정)
    env = SizingEnv(df, max_weight=0.25, drawdown_penalty=0.0, cost_bp=100)
    env.reset()
    obs, r1, *_ = env.step(0.25)  # A: Δw 0.25, 비용 0.01·0.25/3(스케일)
    assert r1 == pytest.approx(-0.01 * 0.25 / 3) and obs[14] == 0.0  # 다음은 B, B 의 직전 비중 0
    obs, r2, *_ = env.step(0.25)  # B: 직전 비중은 B 의 0 → 비용 다시 발생
    assert r2 == pytest.approx(-0.01 * 0.25 / 3) and obs[14] == pytest.approx(0.25)  # 다음 A
    _, r3, *_ = env.step(0.25)  # A: 직전 A 비중 0.25 → 회전 0
    assert r3 == pytest.approx(0.0)


def test_env_uses_per_row_horizon_column():
    df = _steps([0.10, 0.10])
    df["horizon_days"] = [1, 5]
    env = SizingEnv(df, max_weight=0.25, drawdown_penalty=0.0, cost_bp=0, horizon_days=1)
    env.reset()
    _, r1, *_ = env.step(0.25)
    _, r2, *_ = env.step(0.25)
    assert r1 == pytest.approx(0.025) and r2 == pytest.approx(0.025 / 5)


def test_population_rewards_match_episode_loop():
    """CEM 모집단 평가를 한 스텝 루프로 벡터화해도 보상이 루프 구현과 같아야 한다."""
    import numpy as np

    from trader.ml.rl import learners
    from trader.ml.rl.policy import LinearPolicy

    rng = np.random.default_rng(0)
    rets = rng.normal(0, 0.02, 60)
    steps = _steps(rets)
    steps["ticker"] = ["A", "B", "C"] * 20
    env = SizingEnv(steps, max_weight=0.25, drawdown_penalty=1.0, cost_bp=30, horizon_days=5)
    mean, std = learners.obs_stats(steps)
    pop = rng.normal(0, 1, size=(6, OBS_SIZE + 1))
    fast = learners.population_rewards(env, pop, mean=mean, std=std, max_weight=0.25)
    slow = np.array(
        [
            learners._episode_reward(env, LinearPolicy(th, 0.25, mean=mean, std=std))
            for th in pop
        ]
    )
    assert np.allclose(fast, slow, atol=1e-9)
