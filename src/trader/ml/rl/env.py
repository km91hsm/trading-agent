"""사이징 환경 (스펙 §9.4). gymnasium 없이도 같은 API(reset/step)로 돈다.

관측 = 스텝 관측 14차원 + [현재 비중, 드로다운] = 16차원. 행동 = 비중 0~max_weight.
보상 = w·r/(같은 날짜 스텝 수 × horizon_days) − 회전 비용(cost_bp·|Δw|) − drawdown_penalty·(드로다운
증가분). NAV 는 복리. 같은 날 여러 종목과 horizon 일짜리 전방수익을 순차로 쌓으면 과대 복리가 되므로
하루 포트폴리오 수익 근사로 나눈다(절대 수치는 근사, 정책 간 상대 비교용).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

from trader.config import ConfigError
from trader.ledger.performance import metrics_of
from trader.ml.rl.dataset import OBS_COLUMNS

OBS_SIZE = len(OBS_COLUMNS) + 2  # + position_weight + drawdown
POSITION_IDX = len(OBS_COLUMNS)
DRAWDOWN_IDX = len(OBS_COLUMNS) + 1


@dataclass(frozen=True)
class Box:
    """gymnasium.spaces.Box 의 최소 대체물 (shape·low·high 만)."""

    low: np.ndarray
    high: np.ndarray
    shape: tuple[int, ...]


class SizingEnv:
    def __init__(
        self,
        steps: pd.DataFrame,
        *,
        max_weight: float,
        drawdown_penalty: float,
        cost_bp: int,
        horizon_days: int = 1,
    ) -> None:
        if steps is None or len(steps) == 0:
            raise ValueError("RL 환경에 스텝이 없습니다")
        self.obs_matrix = np.asarray([list(o) for o in steps["obs"]], dtype="float64")
        if self.obs_matrix.shape[1] != len(OBS_COLUMNS):
            raise ValueError(f"관측 차원 {self.obs_matrix.shape[1]} ≠ {len(OBS_COLUMNS)}")
        self.returns = np.asarray(steps["fwd_return"], dtype="float64")
        self.dates = [pd.Timestamp(d).date().isoformat() for d in steps["date"]]
        counts = pd.Series(self.dates).value_counts()
        self.scale = np.array(
            [1.0 / (float(counts[d]) * max(int(horizon_days), 1)) for d in self.dates]
        )
        self.max_weight = float(max_weight)
        self.drawdown_penalty = float(drawdown_penalty)
        self.cost = float(cost_bp) / 10_000.0
        self.observation_space = Box(
            np.full(OBS_SIZE, -np.inf), np.full(OBS_SIZE, np.inf), (OBS_SIZE,)
        )
        self.action_space = Box(np.zeros(1), np.full(1, self.max_weight), (1,))
        self.reset()

    # --- gym API -----------------------------------------------------------------------
    def reset(self, seed: int | None = None, options: dict | None = None):
        self.i = 0
        self.nav = 1.0
        self.peak = 1.0
        self.w_prev = 0.0
        self.dd = 0.0
        self.navs: list[float] = [1.0]
        return self._obs(), {}

    def _obs(self) -> np.ndarray:
        k = min(self.i, len(self.returns) - 1)
        return np.concatenate([self.obs_matrix[k], [self.w_prev, self.dd]]).astype("float64")

    def step(self, action: Any):
        if self.i >= len(self.returns):
            raise ValueError("에피소드가 끝났습니다: reset() 필요")
        w = float(np.asarray(action, dtype="float64").reshape(-1)[0])
        w = min(max(w, 0.0), self.max_weight)
        r = float(self.returns[self.i])
        pnl = w * r * float(self.scale[self.i])
        self.nav *= 1.0 + pnl
        self.peak = max(self.peak, self.nav)
        dd_new = self.nav / self.peak - 1.0 if self.peak > 0 else 0.0
        reward = pnl - self.cost * abs(w - self.w_prev) - self.drawdown_penalty * max(
            0.0, self.dd - dd_new
        )
        self.w_prev, self.dd = w, dd_new
        self.navs.append(self.nav)
        self.i += 1
        terminated = self.i >= len(self.returns)
        info = {"nav": self.nav, "drawdown": self.dd, "weight": w}
        return self._obs(), float(reward), terminated, False, info

    # --- 평가 ---------------------------------------------------------------------------
    def nav_series(self) -> list[tuple[str, Decimal]]:
        labels = [self.dates[0], *self.dates[: len(self.navs) - 1]]
        return [(d, Decimal(str(v))) for d, v in zip(labels, self.navs, strict=True)]


def episode_metrics(env: SizingEnv) -> dict:
    """performance.metrics_of(NAV) + steps(진행한 스텝 수)."""
    return {**metrics_of(env.nav_series()), "steps": env.i}


def gym_env(env: SizingEnv):
    """stable-baselines3 용 gymnasium.Env 어댑터 (선택 extra)."""
    try:
        import gymnasium
        from gymnasium import spaces
    except ImportError as e:  # pragma: no cover - 환경별
        raise ConfigError("RL PPO 백엔드는 `uv sync --extra rl` 이 필요합니다") from e

    class _Gym(gymnasium.Env):
        metadata = {"render_modes": []}

        def __init__(self) -> None:
            super().__init__()
            self.inner = env
            self.observation_space = spaces.Box(-np.inf, np.inf, (OBS_SIZE,), dtype=np.float32)
            self.action_space = spaces.Box(0.0, env.max_weight, (1,), dtype=np.float32)

        def reset(self, *, seed: int | None = None, options: dict | None = None):
            super().reset(seed=seed)
            obs, info = self.inner.reset(seed=seed)
            return obs.astype(np.float32), info

        def step(self, action):
            obs, reward, term, trunc, info = self.inner.step(action)
            return obs.astype(np.float32), reward, term, trunc, info

    return _Gym()
