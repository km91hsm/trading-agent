import numpy as np
import pytest
from helpers_rl import steps_of as _steps

from trader.ml.rl.env import OBS_SIZE, SizingEnv
from trader.ml.rl.policy import FixedPolicy, LinearPolicy, LlmPolicy, VolTargetPolicy, evaluate


def test_fixed_llm_vol_policies():
    obs = np.zeros(OBS_SIZE)
    obs[13] = 0.12
    obs[4] = 0.04
    assert FixedPolicy(0.1).act(obs) == 0.1 and LlmPolicy().act(obs) == pytest.approx(0.12)
    assert VolTargetPolicy(target_vol=0.02, max_weight=0.25).act(obs) == pytest.approx(0.25)
    obs[4] = 0.10
    assert VolTargetPolicy(target_vol=0.02, max_weight=0.25).act(obs) == pytest.approx(0.2)


def test_linear_policy_bounded_and_evaluate_deterministic():
    pol = LinearPolicy(np.full(OBS_SIZE + 1, 5.0), max_weight=0.25)
    assert 0 <= pol.act(np.ones(OBS_SIZE)) <= 0.25
    env = SizingEnv(_steps([0.01, 0.02, -0.01]), max_weight=0.25, drawdown_penalty=1.0, cost_bp=30)
    a, b = evaluate(env, pol), evaluate(env, pol)
    assert a == b and a["steps"] == 3 and a["return"] is not None
