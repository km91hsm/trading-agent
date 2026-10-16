"""스크리너 랭킹 (스펙 §9.2): 승격된 퀀트 모델의 P(up) 으로 scan 후보를 재정렬한다.

필터(거래대금·변동성)는 정량 규칙 그대로 두고 점수만 z(P(up))·0.6 + z(정량)·0.4 로 바꾼다.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd

from trader.ml import registry
from trader.ml.models import quant as quant_model
from trader.scan import quant as scan_quant

ML_WEIGHT = 0.6


def _z(s: pd.Series) -> pd.Series:
    sd = s.std(ddof=0)
    if not np.isfinite(sd) or sd == 0:
        return pd.Series(0.0, index=s.index)
    return (s - s.mean()) / sd


def current(state: Path | None = None) -> tuple[Callable[[pd.DataFrame], pd.DataFrame], str] | None:
    """승격 모델이 있으면 (rank_fn, "quant@버전"), 없으면 None."""
    found = registry.latest("quant", state=state)
    if found is None:
        return None
    model, meta = found
    cols = list(meta.get("features") or model.features)
    label = f"{meta.get('name', 'quant')}@{meta.get('version', '?')}"

    def rank_fn(df: pd.DataFrame) -> pd.DataFrame:
        base = scan_quant.score(df, top=len(df) if len(df) else 1)
        if base.empty:
            return base
        rows = df.set_index("ticker").loc[base["ticker"]]
        p_up = model.predict_proba(quant_model.align(rows.reset_index(), cols))[:, 2]
        base = base.copy()
        base["p_up"] = np.round(p_up, 4)
        base["score"] = (_z(base["p_up"]) * ML_WEIGHT + _z(base["score"]) * (1 - ML_WEIGHT)).round(
            4
        )
        base["reasons"] = [
            [*r, f"ML P(up)={p:.2f}"] for r, p in zip(base["reasons"], base["p_up"], strict=True)
        ]
        out = base.sort_values("score", ascending=False).head(scan_quant.TOP_N)
        return out.reset_index(drop=True)

    return rank_fn, label


def rank_fn_or_none(state: Path | None = None) -> Callable[[pd.DataFrame], pd.DataFrame] | None:
    c = current(state)
    return c[0] if c else None
