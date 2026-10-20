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


def population_rewards(
    env: SizingEnv,
    pop: np.ndarray,
    *,
    mean: np.ndarray,
    std: np.ndarray,
    max_weight: float,
) -> np.ndarray:
    """모집단 전체의 에피소드 보상 합을 한 스텝 루프로 (LinearPolicy + SizingEnv.step 과 동치).

    정책의 정적 부분(관측 14열)은 행렬곱 1회로 미리 계산하고, 궤적에 의존하는 두 항(직전 비중·
    드로다운)만 스텝마다 더한다. 순수 파이썬 스텝 루프(13.6 µs/스텝 × 모집단 64 × 반복 30)가 기본
    설정에서 1~3시간
    걸리던 것을 모집단 크기와 무관한 루프 1회로 줄인다."""
    pop = np.asarray(pop, dtype="float64")
    n_static = OBS_SIZE - 2
    z_static = (env.obs_matrix - mean[:n_static]) / std[:n_static]  # (T, 14)
    lin = z_static @ pop[:, :n_static].T  # (T, P)
    th_w, th_dd, bias = pop[:, n_static], pop[:, n_static + 1], pop[:, -1]
    p_n = pop.shape[0]
    nav = np.ones(p_n)
    peak = np.ones(p_n)
    dd = np.zeros(p_n)
    total = np.zeros(p_n)
    last_w: dict[str, np.ndarray] = {}
    zeros = np.zeros(p_n)
    for i in range(len(env.returns)):
        ticker = env.tickers[i]
        w_prev = last_w.get(ticker, zeros)
        z = (
            lin[i]
            + th_w * ((w_prev - mean[n_static]) / std[n_static])
            + th_dd * ((dd - mean[n_static + 1]) / std[n_static + 1])
            + bias
        )
        w = np.clip(max_weight / (1.0 + np.exp(-z)), 0.0, env.max_weight)
        scale = float(env.scale[i])
        pnl = w * float(env.returns[i]) * scale
        nav = nav * (1.0 + pnl)
        peak = np.maximum(peak, nav)
        dd_new = np.where(peak > 0, nav / peak - 1.0, 0.0)
        total += pnl - env.cost * np.abs(w - w_prev) * scale
        total -= env.drawdown_penalty * np.maximum(0.0, dd - dd_new)
        last_w[ticker] = w
        dd = dd_new
    return total


def obs_stats(steps: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """학습 스텝 관측 14열의 평균·표준편차(0 이면 1). 뒤 2차원(비중·드로다운)은 0·1 로 둔다."""
    m = np.asarray([list(o) for o in steps["obs"]], dtype="float64")
    mean = np.zeros(OBS_SIZE)
    std = np.ones(OBS_SIZE)
    mean[: m.shape[1]] = m.mean(axis=0)
    sd = m.std(axis=0)
    std[: m.shape[1]] = np.where(sd > 1e-9, sd, 1.0)
    return mean, std


def fit_cem(steps: pd.DataFrame, cfg: RlSizerCfg, *, seed: int) -> LinearPolicy:
    """모집단 cem_population 개의 theta 를 뽑아 보상 합 상위 10% 의 평균·표준편차로 갱신.
    관측은 열별 z-score 로 표준화해 원시 스케일(atr_pct 0.01~0.05 등)이 탐색을 막지 않게 한다."""
    rng = np.random.default_rng(seed)
    env = _env(steps, cfg)
    o_mean, o_std = obs_stats(steps)
    dim = OBS_SIZE + 1
    mean, std = np.zeros(dim), np.ones(dim)
    n_elite = max(1, int(cfg.cem_population * CEM_ELITE_FRAC))

    def policy(th: np.ndarray) -> LinearPolicy:
        return LinearPolicy(th, cfg.max_weight, mean=o_mean, std=o_std)

    for _ in range(cfg.cem_iterations):
        pop = rng.normal(mean, std, size=(cfg.cem_population, dim))
        scores = population_rewards(env, pop, mean=o_mean, std=o_std, max_weight=cfg.max_weight)
        elite = pop[np.argsort(scores)[::-1][:n_elite]]
        mean = elite.mean(axis=0)
        std = np.maximum(elite.std(axis=0), CEM_MIN_STD)
    return policy(mean)


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


def _sb3_unavailable_reason() -> str | None:
    """PPO 를 못 쓰는 이유(없으면 None). SB3 import 뿐 아니라 torch↔NumPy 상호운용도 확인한다 —
    Intel macOS 는 마지막 휠 torch 2.2.2 가 NumPy 1 로 빌드돼 NumPy 2 환경에서 import 는 되지만
    학습 중 'Numpy is not available' 로 죽는다."""
    try:
        import_module("stable_baselines3")
    except ImportError:
        return "RL PPO 백엔드는 `uv sync --extra rl` 이 필요합니다"
    try:
        torch = import_module("torch")
        torch.from_numpy(np.zeros(1, dtype="float32"))
    except Exception as e:  # noqa: BLE001 - RuntimeError('Numpy is not available') 등
        return f"torch 가 NumPy 와 맞지 않아 PPO 를 쓸 수 없습니다 (torch 2.2.2 + NumPy 2): {e}"
    return None


def _sb3_available() -> bool:
    return _sb3_unavailable_reason() is None


def resolve_backend(cfg: RlSizerCfg) -> Literal["cem", "ppo"]:
    """auto → SB3·torch 가 쓸 수 있으면 ppo, 아니면 cem. ppo 지정인데 못 쓰면 ConfigError."""
    if cfg.backend == "cem":
        return "cem"
    reason = _sb3_unavailable_reason()
    if cfg.backend == "ppo":
        if reason is not None:
            raise ConfigError(reason)
        return "ppo"
    if reason is not None:
        logger.info("RL 백엔드 auto → cem (%s)", reason)
        return "cem"
    return "ppo"


def fit(
    steps: pd.DataFrame, cfg: RlSizerCfg, *, seed: int, backend: str | None = None
) -> tuple[Policy, str]:
    """(정책, 실제 백엔드)."""
    b = backend or resolve_backend(cfg)
    if b == "ppo":
        return fit_ppo(steps, cfg, seed=seed), "ppo"
    return fit_cem(steps, cfg, seed=seed), "cem"
