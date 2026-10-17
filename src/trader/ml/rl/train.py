"""RL 사이저 학습 파이프라인 (스펙 §9.4·9.5).

스텝 → 워크포워드 폴드(라벨 엠바고 동일) × 시드 → 테스트 창에서 기준선 3종과 비교 →
전체 스텝으로 최종 적합 → 레지스트리 `rl_sizer` 저장. **자동 승격은 없다**(`trader rl promote`).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from trader.config import MlConfig, RlSizerCfg
from trader.ml import registry, walkforward
from trader.ml.rl import learners
from trader.ml.rl.dataset import OBS_COLUMNS, build_steps
from trader.ml.rl.env import SizingEnv
from trader.ml.rl.policy import FixedPolicy, LlmPolicy, Policy, VolTargetPolicy, evaluate

logger = logging.getLogger("trader.ml")

NAME = "rl_sizer"
MANUAL_ONLY = "수동 승격 전용: trader rl promote <버전>"
POLICIES = ("rl", "fixed", "llm", "vol_target")
FitFn = Callable[..., tuple[Policy, str]]


def _default_fit(steps: pd.DataFrame, rl_cfg: RlSizerCfg, *, seed: int) -> tuple[Policy, str]:
    return learners.fit(steps, rl_cfg, seed=seed)


def _env(steps: pd.DataFrame, rl: RlSizerCfg) -> SizingEnv:
    return SizingEnv(
        steps, max_weight=rl.max_weight, drawdown_penalty=rl.drawdown_penalty, cost_bp=rl.cost_bp
    )


def _mean(xs: list) -> float | None:
    vals = [float(x) for x in xs if x is not None]
    return round(float(np.mean(vals)), 6) if vals else None


def _std(xs: list) -> float | None:
    vals = [float(x) for x in xs if x is not None]
    return round(float(np.std(vals)), 6) if vals else None


def _summarize(rows: dict[str, list[dict]]) -> dict:
    out = {}
    for name, ms in rows.items():
        out[name] = {
            "n": len(ms),
            "return_mean": _mean([m.get("return") for m in ms]),
            "return_std": _std([m.get("return") for m in ms]),
            "mdd_mean": _mean([m.get("mdd") for m in ms]),
            "sortino_mean": _mean([m.get("sortino") for m in ms]),
        }
    return out


def train_sizer(
    cfg: MlConfig,
    conn: sqlite3.Connection,
    *,
    state: Path | None = None,
    market: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    fit_fn: FitFn | None = None,
) -> dict:
    """반환: status(trained|insufficient_data)·version·backend·source·steps·seeds·folds·summary·
    promoted(False)·promote_reason."""
    rl = cfg.models.rl_sizer
    fit = fit_fn or _default_fit
    steps = build_steps(
        conn,
        state,
        market=market,
        horizon_days=cfg.label.horizon_days,
        min_memory_steps=rl.min_memory_steps,
    )
    base = {
        "status": "insufficient_data",
        "version": None,
        "backend": None,
        "source": str(steps["source"].iloc[0]) if len(steps) else None,
        "steps": int(len(steps)),
        "seeds": rl.seeds,
        "folds": [],
        "summary": {},
        "promoted": False,
        "promote_reason": None,
    }
    if len(steps) < rl.min_steps:
        return base | {"promote_reason": f"데이터 부족: 스텝 {len(steps)} (최소 {rl.min_steps})"}
    folds = walkforward.folds(
        steps["date"],
        n_folds=cfg.train.walk_forward_folds,
        horizon_days=cfg.label.horizon_days,
        embargo_days=cfg.label.embargo_days,
    )
    if not folds:
        return base | {"promote_reason": "워크포워드 폴드를 만들 수 없음(날짜 수 부족)"}
    dates = pd.to_datetime(steps["date"]).dt.date
    fold_rows: list[dict] = []
    collected: dict[str, list[dict]] = {p: [] for p in POLICIES}
    backend_used: str | None = None
    for f in folds:
        train = steps[dates <= f.train_end]
        test = steps[(dates >= f.test_start) & (dates <= f.test_end)]
        if train.empty or test.empty:
            continue
        seed_rows = []
        for seed in range(rl.seeds):
            policy, backend_used = fit(train, rl, seed=seed)
            m = evaluate(_env(test, rl), policy, seed=seed)
            seed_rows.append({"seed": seed, "rl": m})
            collected["rl"].append(m)
        baselines = {
            "fixed": evaluate(_env(test, rl), FixedPolicy(rl.fixed_weight)),
            "llm": evaluate(_env(test, rl), LlmPolicy()),
            "vol_target": evaluate(_env(test, rl), VolTargetPolicy(max_weight=rl.max_weight)),
        }
        for k, m in baselines.items():
            collected[k].append(m)
        fold_rows.append(
            {
                "index": f.index,
                "train_steps": int(len(train)),
                "test_steps": int(len(test)),
                "train_end": f.train_end.isoformat(),
                "test_start": f.test_start.isoformat(),
                "test_end": f.test_end.isoformat(),
                "seeds": seed_rows,
                "baselines": baselines,
            }
        )
    if not fold_rows:
        return base | {"promote_reason": "폴드에 학습·테스트 스텝이 없음"}
    summary = _summarize(collected)
    policy, backend_used = fit(steps, rl, seed=0)
    meta = {
        "market": market,
        "source": base["source"],
        "steps": int(len(steps)),
        "seeds": rl.seeds,
        "backend": backend_used,
        "horizon_days": cfg.label.horizon_days,
        "train_range": [dates.min().isoformat(), dates.max().isoformat()],
        "folds": fold_rows,
        "summary": summary,
        "obs_columns": list(OBS_COLUMNS),
        "cfg": rl.model_dump(),
        "promote_reason": MANUAL_ONLY,
    }
    version = registry.save(NAME, policy, meta, state=state, now=now)
    logger.info(
        "rl_sizer 학습 %s: RL 수익 평균 %s / 고정 %s (승격 없음)",
        version,
        summary["rl"]["return_mean"],
        summary["fixed"]["return_mean"],
    )
    return base | {
        "status": "trained",
        "version": version,
        "backend": backend_used,
        "folds": fold_rows,
        "summary": summary,
        "promote_reason": MANUAL_ONLY,
    }
