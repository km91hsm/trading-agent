"""학습기 (스펙 §9.4). 백엔드 두 가지, 같은 Policy 인터페이스.

- `cem`: 선형 정책을 교차 엔트로피 방법으로 학습 (numpy 만, 플랫폼 동일 동작, 기본).
- `ppo`: stable-baselines3 PPO (선택 extra `rl`; 이 머신에 torch 휠이 없을 수 있다).
"""

from __future__ import annotations

import io
import logging
from importlib import import_module
from typing import Literal

import numpy as np
import pandas as pd

from trader.config import ConfigError, RlSizerCfg
from trader.ml.rl.env import OBS_SIZE, SizingEnv, gym_env
from trader.ml.rl.policy import LinearPolicy, Policy

logger = logging.getLogger("trader.ml")

CEM_ELITE_FRAC = 0.1
CEM_MIN_STD = 0.05


def _env(steps: pd.DataFrame, cfg: RlSizerCfg) -> SizingEnv:
    return SizingEnv(
        steps,
        max_weight=cfg.max_weight,
        drawdown_penalty=cfg.drawdown_penalty,
        cost_bp=cfg.cost_bp,
        horizon_days=cfg.horizon_days,
    )


def _episode_reward(env: SizingEnv, policy: Policy) -> float:
    obs, _ = env.reset()
    total, done = 0.0, False
    while not done:
        obs, r, done, _, _ = env.step(policy.act(obs))
        total += r
    return total


def fit_cem(steps: pd.DataFrame, cfg: RlSizerCfg, *, seed: int) -> LinearPolicy:
    """모집단 cem_population 개의 theta 를 뽑아 보상 합 상위 10% 의 평균·표준편차로 갱신."""
    rng = np.random.default_rng(seed)
    env = _env(steps, cfg)
    dim = OBS_SIZE + 1
    mean, std = np.zeros(dim), np.ones(dim)
    n_elite = max(1, int(cfg.cem_population * CEM_ELITE_FRAC))
    for _ in range(cfg.cem_iterations):
        pop = rng.normal(mean, std, size=(cfg.cem_population, dim))
        scores = np.array(
            [_episode_reward(env, LinearPolicy(th, cfg.max_weight)) for th in pop]
        )
        elite = pop[np.argsort(scores)[::-1][:n_elite]]
        mean = elite.mean(axis=0)
        std = np.maximum(elite.std(axis=0), CEM_MIN_STD)
    return LinearPolicy(theta=mean, max_weight=cfg.max_weight)


class PpoPolicy:
    """SB3 PPO 래퍼. pickle 에는 모델 zip 바이트만 담아 joblib 저장이 가능하다."""

    def __init__(self, model_bytes: bytes, *, max_weight: float) -> None:
        self.model_bytes = model_bytes
        self.max_weight = float(max_weight)
        self._model = None

    def _load(self):
        if self._model is None:
            from stable_baselines3 import PPO

            self._model = PPO.load(io.BytesIO(self.model_bytes), device="cpu")
        return self._model

    def act(self, obs: np.ndarray) -> float:
        a, _ = self._load().predict(np.asarray(obs, dtype=np.float32), deterministic=True)
        w = float(np.asarray(a).reshape(-1)[0])
        return min(max(w, 0.0), self.max_weight)

    def __getstate__(self) -> dict:
        return {"model_bytes": self.model_bytes, "max_weight": self.max_weight}

    def __setstate__(self, state: dict) -> None:
        self.model_bytes = state["model_bytes"]
        self.max_weight = state["max_weight"]
        self._model = None


def fit_ppo(steps: pd.DataFrame, cfg: RlSizerCfg, *, seed: int) -> PpoPolicy:
    try:
        from stable_baselines3 import PPO
    except ImportError as e:
        raise ConfigError("RL PPO 백엔드는 `uv sync --extra rl` 이 필요합니다") from e
    model = PPO("MlpPolicy", gym_env(_env(steps, cfg)), seed=seed, verbose=0, device="cpu")
    model.learn(total_timesteps=cfg.total_timesteps)
    buf = io.BytesIO()
    model.save(buf)
    return PpoPolicy(buf.getvalue(), max_weight=cfg.max_weight)


def _sb3_available() -> bool:
    try:
        import_module("stable_baselines3")
    except ImportError:
        return False
    return True


def resolve_backend(cfg: RlSizerCfg) -> Literal["cem", "ppo"]:
    """auto → SB3 가 import 되면 ppo, 아니면 cem. ppo 지정인데 없으면 ConfigError."""
    if cfg.backend == "cem":
        return "cem"
    if cfg.backend == "ppo":
        if not _sb3_available():
            raise ConfigError("RL PPO 백엔드는 `uv sync --extra rl` 이 필요합니다")
        return "ppo"
    return "ppo" if _sb3_available() else "cem"


def fit(
    steps: pd.DataFrame, cfg: RlSizerCfg, *, seed: int, backend: str | None = None
) -> tuple[Policy, str]:
    """(정책, 실제 백엔드)."""
    b = backend or resolve_backend(cfg)
    if b == "ppo":
        return fit_ppo(steps, cfg, seed=seed), "ppo"
    return fit_cem(steps, cfg, seed=seed), "cem"
