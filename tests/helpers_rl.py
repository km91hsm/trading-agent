"""RL 테스트용 스텝 DataFrame 생성기."""

from __future__ import annotations

import pandas as pd
from helpers_ml import trading_days


def steps_of(returns, *, atr_pct: float = 0.02, obs_extra=None) -> pd.DataFrame:
    """1종목, 관측은 atr_pct(인덱스 4) 만 채우고 나머지 0."""
    n = len(returns)
    base = [0.0] * 4 + [atr_pct] + [0.0] * 9
    obs = obs_extra if obs_extra is not None else [list(base) for _ in range(n)]
    return pd.DataFrame(
        {
            "date": pd.to_datetime(trading_days(n)),
            "ticker": "A",
            "obs": obs,
            "fwd_return": list(returns),
            "source": "features",
        }
    )
