"""사이징 정책 (스펙 §9.4): 기준선 3종(고정·LLM 비중·변동성 타깃) + 학습 정책(선형·PPO)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np

from trader.ml.rl.dataset import OBS_COLUMNS
from trader.ml.rl.env import OBS_SIZE, SizingEnv, episode_metrics

ATR_IDX = OBS_COLUMNS.index("atr_pct")
LLM_IDX = OBS_COLUMNS.index("llm_weight")


class Policy(Protocol):
    def act(self, obs: np.ndarray) -> float: ...


@dataclass(frozen=True)
class FixedPolicy:
    weight: float

    def act(self, obs: np.ndarray) -> float:
        return float(self.weight)


@dataclass(frozen=True)
class LlmPolicy:
    """LLM 트레이더의 target_weight 그대로."""

    def act(self, obs: np.ndarray) -> float:
        return float(obs[LLM_IDX])


@dataclass(frozen=True)
class VolTargetPolicy:
    target_vol: float = 0.02
    max_weight: float = 0.25

    def act(self, obs: np.ndarray) -> float:
        atr = max(float(obs[ATR_IDX]), 1e-4)
        return float(min(self.max_weight, self.target_vol / atr))


@dataclass(frozen=True)
class LinearPolicy:
    """max_weight · sigmoid(theta[:-1]·obs + theta[-1]). theta 길이 = OBS_SIZE + 1."""

    theta: np.ndarray
    max_weight: float

    def act(self, obs: np.ndarray) -> float:
        th = np.asarray(self.theta, dtype="float64")
        z = float(th[:-1] @ np.asarray(obs, dtype="float64")[:OBS_SIZE] + th[-1])
        return float(self.max_weight / (1.0 + np.exp(-z)))


def evaluate(env: SizingEnv, policy: Policy, *, seed: int = 0) -> dict:
    """한 에피소드를 결정적으로 돌리고 episode_metrics 를 돌려준다."""
    obs, _ = env.reset(seed=seed)
    done = False
    while not done:
        obs, _, done, _, _ = env.step(policy.act(obs))
    return episode_metrics(env)
